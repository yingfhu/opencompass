"""CLP Inferencer."""

import itertools
import os
from functools import partial
from typing import List, Optional

import torch
import torch.nn.functional as F
from accelerate import Accelerator
from tqdm import trange

from opencompass.models import BaseModel
from opencompass.openicl import PromptTemplate
from opencompass.openicl.icl_inferencer.icl_base_inferencer import \
    PPLInferencerOutputHandler
from opencompass.openicl.icl_retriever import BaseRetriever
from opencompass.openicl.utils.logging import get_logger
from opencompass.registry import ICL_INFERENCERS

logger = get_logger(__name__)


@ICL_INFERENCERS.register_module()
class CLPInferencer:
    """Conditional log probability based In-context Learning Inferencer.

    Calculate the log probability of each choices according the logits.
    The input is the context with single choice, e.g. Q: xx.\n A: first choice
    to this question.
    And starting from the first token of this choice, sum up all the log
    probabilities of each
    tokens from logits. Then, compare each choice with softmax.

    There are two scenarios in this case:
    1. Single token choices. Already supported.
    2. Muiltple token choices. TODO: More complicated and needs to be added in
       the future for specific dataset.

    Attributes:
        model (:obj:`BaseModel`, optional): The module to inference.
        max_seq_len (:obj:`int`): Maximum number of tokenized words allowed by
            the LM.
        batch_size (:obj:`int`, optional): Batch size for the :obj:`DataLoader`
        accelerator (:obj:`Accelerator`, optional): An instance of the
            `Accelerator` class, used for multiprocessing.
        output_json_filepath (:obj:`str`, optional): File path for output
            `JSON` file.
        output_json_filename (:obj:`str`, optional): File name for output
            `JSON` file.
        single_token (:obj:`bool`): If ``True``, choices only have one token to
            calculate. Defaults to True. Currently only support True.
    """

    def __init__(
            self,
            model: BaseModel,
            max_seq_len: Optional[int] = None,
            batch_size: Optional[int] = 1,
            accelerator: Optional[Accelerator] = None,
            output_json_filepath: Optional[str] = './icl_inference_output',
            output_json_filename: Optional[str] = 'predictions',
            fix_id_list: Optional[List[int]] = None,
            single_token: bool = True,
            **kwargs) -> None:

        self.model = model

        self.accelerator = accelerator
        self.is_main_process = (True if self.accelerator is None
                                or self.accelerator.is_main_process else False)

        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        if self.model is not None:
            self.model.to(self.device)

        self.max_seq_len = max_seq_len
        self.batch_size = batch_size
        self.output_json_filepath = output_json_filepath
        self.output_json_filename = output_json_filename
        if not os.path.exists(self.output_json_filepath):
            os.makedirs(self.output_json_filepath)
        self.fix_id_list = fix_id_list
        # TODO: support multiple token
        assert single_token, 'Only support single token choice currently.'
        self.single_token = single_token

    def inference(self,
                  retriever: BaseRetriever,
                  ice_template: Optional[PromptTemplate] = None,
                  prompt_template: Optional[PromptTemplate] = None,
                  output_json_filepath: Optional[str] = None,
                  output_json_filename: Optional[str] = None,
                  normalizing_str: Optional[str] = None) -> List:
        # 1. Preparation for output logs
        output_handler = PPLInferencerOutputHandler()

        ice = []

        if output_json_filepath is None:
            output_json_filepath = self.output_json_filepath
        if output_json_filename is None:
            output_json_filename = self.output_json_filename

        # 2. Get results of retrieval process
        if self.fix_id_list:
            ice_idx_list = retriever.retrieve(self.fix_id_list)
        else:
            ice_idx_list = retriever.retrieve()

        # 3. Generate in-context examples for testing inputs
        for idx in range(len(ice_idx_list)):
            ice.append(
                retriever.generate_ice(ice_idx_list[idx],
                                       ice_template=ice_template))
        output_handler.save_ice(ice)

        # 4. Collect prompts and calculate conditional log probs
        if self.single_token:
            index = 0
            prompt_list = []
            choice_target_ids = []
            # TODO: Hard code temperaily, need to modified here
            choices = retriever.test_ds[0]['choices']
            try:
                choice_ids = [
                    self.model.tokenizer.encode(c, False, False)
                    for c in choices
                ]
            except ValueError:
                choice_ids = [self.model.tokenizer.encode(c) for c in choices]
                if self.model.tokenizer.add_bos_token:
                    choice_ids = [c[1:] for c in choice_ids]
                if self.model.tokenizer.add_eos_token:
                    choice_ids = [c[:-1] for c in choice_ids]
            if isinstance(choice_ids[0], list):
                # in case tokenizer returns list for single token
                choice_ids = list(itertools.chain(*choice_ids))

                get_token_len = partial(
                    self.model.get_token_len,  # COPYBARA_INTERNAL  # noqa
                    eos=False)  # COPYBARA_INTERNAL  # noqa
            get_token_len = self.model.get_token_len

            # prepare in context for each example and control the length
            for idx in range(len(ice_idx_list)):
                prompt = retriever.generate_prompt_for_generate_task(
                    idx,
                    ice[idx],
                    ice_template=ice_template,
                    prompt_template=prompt_template)
                if self.max_seq_len is not None:
                    prompt_token_num = get_token_len(prompt)
                    # add one because additional token will be added in the end
                    while len(
                            ice_idx_list[idx]
                    ) > 0 and prompt_token_num + 1 > self.max_seq_len:
                        ice_idx_list[idx] = ice_idx_list[idx][:-1]
                        ice[idx] = retriever.generate_ice(
                            ice_idx_list[idx], ice_template=ice_template)
                        prompt = retriever.generate_prompt_for_generate_task(
                            idx,
                            ice[idx],
                            ice_template=ice_template,
                            prompt_template=prompt_template)
                        prompt_token_num = get_token_len(prompt)
                # Add single token for prompt, this token can be any token
                prompt += 'yes'
                prompt_list.append(prompt)
                # in case prompt token num reaches
                if self.max_seq_len is not None and \
                        prompt_token_num + 1 > self.max_seq_len:
                    prompt_token_num = self.max_seq_len - 1
                # minus the bos token
                choice_target_ids.append(prompt_token_num - 1)

            logger.info('Calculating conditional log probability for prompts.')
            for idx in trange(0,
                              len(prompt_list),
                              self.batch_size,
                              disable=not self.is_main_process):
                sub_prompt_list = prompt_list[idx:idx + self.batch_size]
                sub_choice_target_ids = choice_target_ids[idx:idx +
                                                          self.batch_size]
                sub_res = self.__get_cond_prob(sub_prompt_list,
                                               sub_choice_target_ids,
                                               choice_ids)

                for res, prompt in zip(sub_res, sub_prompt_list):
                    output_handler.save_prompt_and_condprob(
                        prompt.replace(ice[idx], ''), prompt, res, index,
                        choices)
                    index = index + 1

        # 5. Output
        if self.is_main_process:
            os.makedirs(output_json_filepath, exist_ok=True)
            output_handler.write_to_json(output_json_filepath,
                                         output_json_filename)

        return [
            sample['prediction']
            for sample in output_handler.results_dict.values()
        ]

    def __get_cond_prob(self,
                        input_texts: List[str],
                        sub_choice_target_ids,
                        choice_ids,
                        mask_length=None):
        # TODO: support multiple tokens
        try:
            outputs, _ = self.model.generator.get_logits(input_texts)
        except AttributeError:
            outputs, _ = self.model.get_logits(input_texts)
        shift_logits = outputs[..., :-1, :].contiguous()

        shift_logits = F.log_softmax(shift_logits, dim=-1)
        log_probs = []
        for logits, target_ids in zip(shift_logits, sub_choice_target_ids):
            log_probs.append(
                F.softmax(logits[target_ids, choice_ids], dim=-1).tolist())
        return log_probs

# 高效评测

OpenCompass 支持自定义评测任务的任务划分器（`Partitioner`），实现评测任务的灵活切分；同时配合 `Runner` 控制任务执行的平台，如本机及集群。通过二者的组合，OpenCompass 可以将大评测任务分割到大量计算节点上运行，高效利用计算资源，从而大大加速评测流程。

## 任务划分 (Partitioner)

由于大语言模型的推理耗时长，评测的数据集量大，因此串行运行一次评测任务的时间开销往往很大。
OpenCompass 支持通过自定义评测任务的任务划分器（`Partitioner`），将大评测任务按不同策略划分为众多独立的小任务，通过并行运行充分利用计算资源。用户可以通过 `infer.partitioner` 及 `eval.partitioner` 配置推理和评测阶段的任务划分策略。下面，我们将会介绍 OpenCompass 中支持的所有划分策略。

### `NaivePartitioner`

该划分器会将每个模型和数据集的组合作为一个独立任务派发，为最基础的划分策略，并无任何额外参数。

```python
from opencompass.partitioners import NaivePartitioner

infer = dict(
    partitioner=dict(type=SizePartitioner)
    # ...
)
```

### `SizePartitioner`

```{warning}
该划分器不适用于评测阶段的任务（OpenEvalTask）。
```

该划分器会根据数据集的大小，乘上一个扩张系数，估算该数据集的推理成本（耗时）。然后会通过切分大数据集、合并小数据集的方式创建任务，尽可能保证各个子任务推理成本均等。

该划分器常用的参数如下：

```python
from opencompass.partitioners import SizePartitioner

infer = dict(
    partitioner=dict(
        type=SizePartitioner,
        max_task_size: int = 2000,  # 单个任务的最大长度
        gen_task_coef: int = 20,  # 生成式任务的扩张系数
    ),
    # ...
)
```

`SizePartitioner` 在估算数据集推理成本时, 会根据推理任务的类型，选择不同的扩张系数。对于生成式任务，如使用 `GenInferencer` 的任务，会设置成比较大的 `gen_task_coef`；对于判别式任务，如使用 `PPLInferencer` 的任务，则会设置成 prompt 中 label 的数量。

```{note}
目前这种分割策略实现仍然比较粗糙，并未能准确反映生成式任务与判别式任务的计算量差距。我们也期待社区能提出更好的划分策略 ：）
```

## 运行后端 (Runner)

在多卡多机的集群环境下，我们若想实现多个任务的并行执行，通常需要依赖集群管理系统（如 Slurm）对任务进行分配和调度。OpenCompass 中，任务的分配和运行统一交由 Runner 负责。目前已经支持了 Slurm 和 PAI-DLC 两种调度后端，同时也保留了在本机直接启动任务的 `LocalRunner`。

### `LocalRunner`

`LocalRunner` 为最基本的运行器，可以将任务在本机串行运行。

```python
from opencompass.runners import LocalRunner
from opencompass.tasks import OpenICLInferTask

infer = dict(
    # ...
    runner=dict(
        type=LocalRunner,
        task=dict(type=OpenICLEvalTask),  # 待运行的任务
    )
)
```

在未来，我们计划提升 `LocalRunner` 的能力，实现单机多卡资源的高效利用。

### `SlurmRunner`

`SlurmRunner` 会将任务提交到 Slurm 集群上运行。常用的配置字段如下：

```python
from opencompass.runners import SlurmRunner
from opencompass.tasks import OpenICLInferTask

infer = dict(
    # ...
    runner=dict(
        type=SlurmRunner,
        task=dict(type=OpenICLEvalTask),  # 待运行任务
        max_num_workers=16,  # 最大同时评测任务数
        partition='lm',  # 运行任务的 Slurm 分区
        quotatype='auto',  # （仅在某些 Slurm 中支持，可以不设置）运行任务的优先级
        retry=2,  # 任务失败的重试次数，可以避免意外发生的错误
    ),
)
```

### `DLCRunner`

`DLCRunner` 则可以将任务提交到 Alibaba Deep Learning Ceneter (DLC) 运行，该 Runner 依赖于 dlc。首先，先在环境内准备好 dlc：

```bash
cd ~
wget https://dlc-cli.oss-cn-zhangjiakou.aliyuncs.com/light/binary/linux/amd64/dlc
chmod +x ./dlc
sudo ln -rs dlc /usr/local/bin
./dlc config
```

根据提示填入相应信息，并得到 dlc 的配置文件（如 /user/.dlc/config），即完成了前期准备。之后，我们在配置文件按照格式指定 `DLCRunner` 的配置：

```python
from opencompass.runners import DLCRunner
from opencompass.tasks import OpenICLInferTask

infer = dict(
    # ...
    runner=dict(
        type=DLCRunner,
        task=dict(type=OpenICLEvalTask),  # 待运行任务
        max_num_workers=16,  # 最大同时评测任务数
        aliyun_cfg=dict(
            bashrc_path="/user/.bashrc",  # 用于初始化运行环境的 bashrc 路径
            conda_env_name='opencompass',  # OpenCompass 的 conda 环境
            dlc_config_path="/user/.dlc/config",  # dlc 配置文件
            workspace_id='ws-xxx',  # DLC 工作空间 ID
            worker_image='xxx',  # 运行任务的 image url
        ),
        retry=2,  # 任务失败的重试次数，可以避免意外发生的错误
    ),
)

```

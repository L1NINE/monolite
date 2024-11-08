import sys
import os

import torch.nn.intrinsic

sys.path.append(os.path.abspath("./"))

from lib.utils.logger import logger, build_progress
from lib.models.init import weight_init
from lib.cfg.base import (
    LossBase,
    TrainerBase,
    DataSetBase,
    SchedulerBase,
    OptimizerBase,
    VisualizerBase,
)

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.amp
import numpy as np
import random

torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

import importlib
import argparse
from rich.live import Live
from torchinfo import summary
import time
import swanlab
import datetime
import psutil

try:
    local_rank = int(os.environ["LOCAL_RANK"])
except:
    local_rank = -1
    
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
pid = os.getpid()
pcontext = psutil.Process(pid)
# os.environ["CUDA_LAUNCH_BLOCKING"] = "1"  # 设置同步cuda,仅debug时使用


def train(
    model: torch.nn.Module,
    trainner: TrainerBase,
    device,
    train_loader: torch.utils.data.DataLoader,
    test_loader: torch.utils.data.DataLoader,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.LRScheduler,
    loss_fn: LossBase,
    visualizer: VisualizerBase,
    logger,
):
    scaler = torch.amp.GradScaler()

    # pin random seed
    torch.manual_seed(trainner.get_seed())
    np.random.seed(trainner.get_seed())
    random.seed(trainner.get_seed())

    table, progress, task_ids = build_progress(
        len(train_loader), trainner.get_end_epoch()
    )
    with Live(table, refresh_per_second=10) as live:
        progress["Progress"].update(
            task_ids["jobId_all"],
            completed=trainner.get_start_epoch(),
            total=trainner.get_end_epoch(),
        )
        progress["Info"].update(
            task_ids["jobId_epoch_info"],
            completed=trainner.get_start_epoch(),
            total=trainner.get_end_epoch(),
        )
        for epoch_now in range(trainner.get_start_epoch(), trainner.get_end_epoch()):
            model.train()
            for i, (inputs, targets, data_info) in enumerate(train_loader):
                optimizer.zero_grad()
                inputs = inputs.to(device)
                targets = {key: value.to(device) for key, value in targets.items()}
                with torch.autocast(
                    device_type="cuda" if torch.cuda.is_available() else "cpu", enabled=trainner.is_amp()
                ):
                    forward_time = time.time_ns()
                    outputs = model(inputs)
                    forward_time = (time.time_ns() - forward_time) / 1e6  # ms

                    loss_time = time.time_ns()
                    loss, loss_info = loss_fn(outputs, targets)
                    loss_time = (time.time_ns() - loss_time) / 1e6  # ms

                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()

                info = {
                    "epoch": epoch_now,
                    "micostep": i,
                    "allstep": len(train_loader),
                    "forward_time(ms)": forward_time,
                    "loss_time(ms)": loss_time,
                    "dataload_time(ms)": round(
                        torch.mean(data_info["dataload_time"]).item(), 4
                    ),
                    "loss": round(loss.item(), 2),
                    **loss_info,
                    "cpu(%)": round(pcontext.cpu_percent(), 2),
                    "ram(%)": round(pcontext.memory_percent(), 2),
                    **{f"cuda/{k}": v for k, v in torch.cuda.memory_stats(device=device).items()}, # cuda信息
                }
                swanlab.log(info)

                progress["Progress"].update(
                    task_ids["jobId_microstep"],
                    completed=info["micostep"],
                    total=info["allstep"],
                )
                progress["Info"].update(
                    task_ids["jobId_microstep_info"],
                    completed=info["micostep"],
                    total=info["allstep"],
                )
                progress["Time"].update(
                    task_ids["jobId_datatime_info"], completed=info["dataload_time(ms)"]
                )
                progress["Time"].update(
                    task_ids["jobId_losstime_info"], completed=info["loss_time(ms)"]
                )
                progress["Time"].update(
                    task_ids["jobId_forwardtime_info"],
                    completed=info["forward_time(ms)"],
                )
                progress["Loss"].update(
                    task_ids["jobId_loss_info"], completed=info["loss"]
                )
                progress["System"].update(
                    task_ids["jobId_cpu_info"], completed=info["cpu(%)"]
                )
                progress["System"].update(
                    task_ids["jobId_ram_info"], completed=info["ram(%)"]
                )

            scheduler.step()

            # 保存模型
            if not os.path.exists(trainner.get_save_path()):
                os.mkdir(trainner.get_save_path())
            torch.save(
                {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "scheduler": scheduler.state_dict(),
                    "epoch": epoch_now,
                },
                os.path.join(trainner.get_save_path(), "model.pth"),
            )
            logger.info(
                f"checkpoint: {epoch_now+1} saved to {trainner.get_save_path()}"
            )

            # 保存模型预测可视化结果
            model.eval()
            results = visualizer.decode_output(inputs, outputs)
            results = {key: swanlab.Image(value) for key, value in results.items()}
            swanlab.log(results)

            # 保存真值可视化结果
            results = visualizer.decode_target(inputs, targets)
            results = {key: swanlab.Image(value) for key, value in results.items()}
            swanlab.log(results)

            progress["Progress"].update(
                task_ids["jobId_all"],
                completed=epoch_now + 1,
                total=trainner.get_end_epoch(),
            )
            progress["Info"].update(
                task_ids["jobId_epoch_info"],
                completed=epoch_now + 1,
                total=trainner.get_end_epoch(),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monolite training script")
    parser.add_argument(
        "--cfg",
        dest="cfg",
        default=r"C:\workspace\github\monolite\experiment\monolite_YOLO11_centernet",
        help="path to config file",
    )
    args = parser.parse_args()

    # 初始化swanlab,启动$swanlab watch ./logs
    swanlab.init(
        project="monolite",
        experiment_name=f"{os.path.basename(args.cfg)}_{datetime.datetime.now().strftime('%Y/%m/%d_%H:%M:%S')}",
        # logdir="./logs", # 本地模式
        # mode="local",
    )

    # 添加模块搜索路径
    sys.path.append(args.cfg)

    # 导入训练配置
    trainner: TrainerBase = importlib.import_module("trainner").trainner()

    # 导入模型
    model: torch.nn.Module = importlib.import_module("model").model()
    # model = torch.compile(model) # Not support in windows
    model = model.to(device)
    
    # 导入数据集
    data_set: DataSetBase = importlib.import_module("dataset").data_set()

    # 导入优化器
    optimizer: OptimizerBase = importlib.import_module("optimizer").optimizer(model)
    optimizer: torch.optim.Optimizer = optimizer.get_optimizer()

    # 导入学习率衰减器
    scheduler: SchedulerBase = importlib.import_module("scheduler").scheduler(optimizer)
    scheduler: torch.optim.lr_scheduler.LRScheduler = scheduler.get_scheduler()

    # 导入损失函数
    loss: LossBase = importlib.import_module("loss").loss()

    # 导入可视化工具
    visualizer: VisualizerBase = importlib.import_module("visualizer").visualizer()

    # 从checkpoint恢复训练
    if trainner.get_resume_checkpoint() == None:
        model.apply(weight_init)  # 权重初始化
    else:
        logger.info(f"resume from {trainner.get_resume_checkpoint()}")
        checkpoint_dict = torch.load(
            trainner.get_resume_checkpoint(),
            map_location=device,
            weights_only=True,
        )
        model.load_state_dict(checkpoint_dict["model"])
        optimizer.load_state_dict(checkpoint_dict["optimizer"])
        scheduler.load_state_dict(checkpoint_dict["scheduler"])
        trainner.set_start_epoch(checkpoint_dict["epoch"])

    # 打印基本信息
    print(
        f"\n{summary(model, input_size=(data_set.get_bath_size(),3,384,1280),mode='train',verbose=0,depth=2)}"
    )
    logger.info(data_set)
    logger.info(optimizer)
    logger.info(scheduler)
    
    train(
        model,
        trainner,
        device,
        data_set.get_train_loader(),
        data_set.get_test_loader(),
        optimizer,
        scheduler,
        loss,
        visualizer,
        logger,
    )

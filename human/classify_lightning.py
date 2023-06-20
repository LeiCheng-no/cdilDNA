import os
import wandb
import torch
import argparse
import yaml
import numpy as np
import pandas as pd
from torch import nn, optim
from tqdm import tqdm
from omegaconf import OmegaConf
from functools import lru_cache
from datetime import datetime
from torch.utils.data import DataLoader
from sklearn.metrics import roc_auc_score, average_precision_score, accuracy_score
from utils import *
from revolution_models import Classifier
# from chordmixer import Classifier
from torch.utils.data import Dataset
from data_utils import vcf_Dataset
# from model_former_ende import *
from torch.distributed import init_process_group, destroy_process_group
import pytorch_lightning as pl
import matplotlib.pyplot as plt
from transformers import get_cosine_schedule_with_warmup
# from pytorch_lightning.profiler import SimpleProfiler
from pytorch_lightning.strategies import DDPStrategy
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.utilities.model_summary import ModelSummary
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor, StochasticWeightAveraging, TQDMProgressBar
from sklearn.metrics import roc_auc_score
from x_formers import FormerClassifier
# from deepsea import Classifier
pl.seed_everything(42)

class LightningWrapper(pl.LightningModule):
    def __init__(self, model, cfg, snapshot_path, train_set, val_set, pretrained, loss):
        super().__init__()
        self.save_hyperparameters(cfg)
        self.model_config = self.hparams.Revolution
        self.batch_size = self.hparams.training.batch_size
        self.model = model(**self.model_config)#.apply(self._init_weights)
        self.save_every = self.hparams.training.save_every
        self.snapshot_path = snapshot_path
        self.train_set = train_set
        self.val_set = val_set
        self.loss = loss

        print(self.model)

        if pretrained:
            pretrained_path = './models/pretrain/model_9_8_16.pt'
            pretrained = torch.load(pretrained_path, map_location='cpu')
            pretrained = pretrained["MODEL_STATE"]

            # for k, v in pretrained.items():
            #     print("pretrained", k)

            from collections import OrderedDict
            new_state_dict = OrderedDict()

            for k, v in pretrained.items():
                if k.startswith('encoder'):
                    new_state_dict[k] = v

            # print("pretrained", new_state_dict.keys())

            net_dict = self.model.state_dict()
            for k, v in new_state_dict.items():
                if k in net_dict:
                    print(k)
            pretrained_cm = {k: v for k, v in new_state_dict.items() if k in net_dict}

            net_dict.update(pretrained_cm)
            self.model.load_state_dict(net_dict)
            for k, v in self.model.state_dict().items():
                print(k, v)
            print("*************pretrained model loaded***************")

        # if os.path.exists(snapshot_path):
        #     print("Loading snapshot")
        #     self._load_snapshot(snapshot_path)

    def forward(self, x):
        # in lightning, forward defines the prediction/inference actions
        return self.model(x)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            m.bias.data.fill_(0.01)

    def training_step(self, batch, batch_idx):
        ref, alt, tissue, label = batch
        output = self.model(ref, alt, tissue).squeeze()
        train_loss = self.loss(output, label)
        # for t, p, l in zip(tissue, output, label):
        #     self.train_preds[t.item()].append(p.item())
        #     self.train_labels[t.item()].append(l.item())
        # if self.global_rank == 0 and self.global_step % self.save_every == 0:
        #     self._save_snapshot()
        return {"loss":train_loss, "preds":output, "labels":label, "tissue":tissue}

    def validation_step(self, batch, batch_idx):
        ref, alt, tissue, label = batch
        output = self.model(ref, alt, tissue).squeeze()
        val_loss = self.loss(output, label)
        return {"loss":val_loss, "preds":output, "labels":label, "tissue":tissue}


    def training_epoch_end(self, outputs):
        train_preds = [[] for _ in range(self.model_config.output_size)]
        train_labels = [[] for _ in range(self.model_config.output_size)]
        train_loss = torch.stack([x["loss"] for x in outputs]).mean()
        tissue = torch.stack([x["tissue"] for x in outputs]).reshape((-1,))
        label = torch.stack([x["labels"] for x in outputs]).reshape((-1,))
        output = torch.stack([x["preds"] for x in outputs]).reshape((-1,))

        for t, p, l in zip(tissue, output, label):
            t = t.to(torch.int8)
            train_preds[t.item()].append(p.item())
            train_labels[t.item()].append(l.item())
        train_rocs = []
        for i in range(self.model_config.output_size):
            train_rocs.append(roc_auc_score(train_labels[i], train_preds[i]))
        train_roc = np.average(train_rocs)
        # auroc = roc_auc_score(outputs[2].detach().cpu().numpy(), outputs[1].detach().cpu().numpy())
        self.log('train_roc', train_roc, sync_dist=True)
        self.log('train_loss', train_loss, sync_dist=True)
        # return {'loss': train_loss}


    def validation_epoch_end(self, outputs):
        size = 0
        val_preds = [[] for _ in range(self.model_config.output_size)]
        val_labels = [[] for _ in range(self.model_config.output_size)]
        val_loss = torch.stack([x["loss"] for x in outputs]).mean()
        tissue = torch.stack([x["tissue"] for x in outputs]).reshape((-1,))
        label = torch.stack([x["labels"] for x in outputs]).reshape((-1,))
        output = torch.stack([x["preds"] for x in outputs]).reshape((-1,))
        # train_preds = torch.stack([x['preds'] for x in outputs])
        # train_labels = torch.stack([x['labels'] for x in outputs])
        i=0
        for t, p, l in zip(tissue, output, label):
            i+=1
            t = t.to(torch.int8)
            val_preds[t.item()].append(p.item())
            val_labels[t.item()].append(l.item())
        print(f"***********{i}")
        val_rocs = []
        for i in range(self.model_config.output_size):
            # print(f"********{i}")
            # print(len(val_labels[i]))
            # print(len(val_preds[i]))
            size+=len(val_preds[i])
            # if len(val_labels[i]) <= 10:
            #     print(val_labels[i], val_preds[i])
            if len(val_labels[i]) != 0 and sum(val_labels[i]) != len(val_labels[i]) and sum(val_labels[i]) != 0:
                val_rocs.append(roc_auc_score(val_labels[i], val_preds[i]))
        val_roc = np.average(val_rocs)
        print(size)
        print(val_loss, val_roc)
        # acc = self.val_acc(validation_step_outputs[2], validation_step_outputs[1])
        # auroc = roc_auc_score(validation_step_outputs[2].detach().cpu().numpy(), validation_step_outputs[1])
        self.log("val_auroc", val_roc, sync_dist=True)
        self.log('val_loss', val_loss, sync_dist=True)
        self.val_preds = [[] for _ in range(self.model_config.output_size)]
        self.val_labels = [[] for _ in range(self.model_config.output_size)]
        # return {'val_loss': val_loss, 'auroc': val_roc}


    def _save_snapshot(self):
        snapshot = {
            "MODEL_STATE": self.model.state_dict(),
            "EPOCHS_RUN": self.current_epoch ,
        }
        torch.save(snapshot, self.snapshot_path)
        print(f"Epoch {self.current_epoch } | Training snapshot saved at {self.snapshot_path}")

    def _load_snapshot(self, snapshot_path):
        loc = f"cuda:0"
        snapshot = torch.load(snapshot_path, map_location=loc)
        self.model.load_state_dict(snapshot["MODEL_STATE"])
        self.epochs_run = snapshot["EPOCHS_RUN"]
        print(f"Resuming training from snapshot at Epoch {self.epochs_run}")

    def train_dataloader(self):
        return DataLoader(
            dataset=self.train_set,
            num_workers=1,
            pin_memory=True,
            shuffle=True,
            drop_last=True,
            batch_size=self.batch_size
            )

    def val_dataloader(self):
        return DataLoader(
            dataset=self.val_set,
            num_workers=1,
            pin_memory=True,
            shuffle=False,
            drop_last=True,
            batch_size=self.batch_size
            )

    def test_dataloader(self):
        return DataLoader(
            dataset=self.test_set,
            num_workers=1,
            pin_memory=True,
            shuffle=False,
            drop_last=True,
            batch_size=self.batch_size
            )

    @lru_cache
    def total_steps(self):
        l = len(self.trainer._data_connector._train_dataloader_source.dataloader())
        print('Num devices', self.trainer.num_devices)
        max_epochs = self.trainer.max_epochs
        accum_batches = self.trainer.accumulate_grad_batches
        manual_total_steps = (l // accum_batches * max_epochs)/self.trainer.num_devices
        print('MANUAL Total steps', manual_total_steps)
        return manual_total_steps

    def configure_optimizers(self):
        optimizer = optim.AdamW(
            self.parameters(),
            lr=self.hparams.training.learning_rate,
            weight_decay=self.hparams.training.weight_decay
        )
        lr_scheduler = get_cosine_schedule_with_warmup(
                    optimizer,
                    num_warmup_steps=int(self.total_steps()*0.3),
                    num_training_steps=self.total_steps(),
                    num_cycles=self.hparams.training.n_cycles
        )
        return [optimizer], [{"scheduler": lr_scheduler, "interval": "step"}]


def classify_main(cfg, key):
    pretrained = cfg.Fine_tuning.training.pretrained
    length = cfg.Fine_tuning.Revolution.max_len

    # with torch.cuda.amp.autocast():
    loss = nn.BCEWithLogitsLoss()
    # x = torch.load(f"./data/ref_{length}_{key}_train_new.pt")
    # print(x.shape)
    train_set = vcf_Dataset(torch.load(f"./data/ref_{length}_{key}_train_new.pt"), torch.load(f"./data/alt_{length}_{key}_train_new.pt"), torch.load(f"./data/tissue_{length}_{key}_train_new.pt"), torch.load(f"./data/label_{length}_{key}_train_new.pt"))
    val_set = vcf_Dataset(torch.load(f"./data/ref_{length}_{key}_test_new.pt"), torch.load(f"./data/alt_{length}_{key}_test_new.pt"), torch.load(f"./data/tissue_{length}_{key}_test_new.pt"), torch.load(f"./data/label_{length}_{key}_test_new.pt"))
    # test_set = vcf_Dataset(torch.load(f"./data/ref_{length}_{key}_test_new.pt"), torch.load(f"./data/alt_{length}_{key}_test_new.pt"), torch.load(f"./data/tissue_{length}_{key}_test_new.pt"), torch.load(f"./data/label_{length}_{key}_test_new.pt"))

    ddp = DDPStrategy(process_group_backend="nccl", find_unused_parameters=True)
    # profiler = SimpleProfiler()
    snapshot_path = "test_new.pt"

    model = LightningWrapper(Classifier, cfg.Fine_tuning, snapshot_path, train_set, val_set, pretrained, loss)
    summary = ModelSummary(model, max_depth=-1)


    # ------------
    # init trainer
    # ------------

    wandb_logger = WandbLogger(dir="./wandb/", project="Chord_VE4_100k", entity='tonyu', name=f'chr11_{length}_{pretrained}')
    checkpoint_callback = ModelCheckpoint(monitor="val_auroc", mode="max")


    print(key, len(train_set), len(val_set))

    lr_monitor = LearningRateMonitor(logging_interval='step')
    callbacks_for_trainer = [TQDMProgressBar(refresh_rate=10), lr_monitor, checkpoint_callback]
    if cfg.Fine_tuning.training.patience != -1:
        early_stopping = EarlyStopping(monitor="val_auroc", mode="max", min_delta=0., patience=cfg.Fine_tuning.training.patience)
        callbacks_for_trainer.append(early_stopping)
    if cfg.Fine_tuning.training.swa_lrs != -1:
        swa = StochasticWeightAveraging(swa_lrs=1e-2)
        callbacks_for_trainer.append(swa)

    print(summary)
    trainer = pl.Trainer(
        check_val_every_n_epoch=1,
        enable_progress_bar=True,
        accelerator='gpu',
        strategy=ddp,
        devices=[0],
        max_epochs=cfg.Fine_tuning.training.n_epochs,
        gradient_clip_val=0.5,
        num_sanity_val_steps=0,
        # profiler=profiler,
        precision=16,
        logger=wandb_logger
    )
    trainer.fit(model)


if __name__ == "__main__":
    cfg = OmegaConf.load('./config/config.yaml')
    OmegaConf.set_struct(cfg, False)
    classify_main(cfg, "chr11")

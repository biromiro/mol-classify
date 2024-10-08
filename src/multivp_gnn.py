# SPDX-FileCopyrightText: Copyright (c) 2023 - 2024 NVIDIA CORPORATION & AFFILIATES.
# SPDX-FileCopyrightText: All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hydra
from omegaconf import DictConfig
import torch
import numpy as np
import pandas as pd

import matplotlib.pyplot as plt
from hydra.utils import to_absolute_path
import torch.nn.functional as F
from torch_geometric.loader import DataLoader
import torch.nn.functional as F

from profile_to_graph import ProfilesToGraphDataset
from torch_geometric.data import Batch

from model import GNN
from modulus.launch.logging import LaunchLogger
from modulus.launch.utils.checkpoint import save_checkpoint
# from modulus.sym.eq.pdes.diffusion import Diffusion

# from utils import HDF5MapStyleDataset
from ops import dx, ddx

# CUDA support
if torch.cuda.is_available():
    device = torch.device("cuda")
else:
    device = torch.device("cpu")

class TensorRobustScaler:
    def __init__(self):
        self.median = None
        self.iqr = None

    def fit(self, X):
        X = X.view(-1)
        self.median = torch.quantile(X, 0.5, dim=-1)
        q1 = torch.quantile(X, 0.25, dim=-1)
        q3 = torch.quantile(X, 0.75, dim=-1)
        self.iqr = q3 - q1

    def transform(self, X):
        return (X - self.median) / self.iqr

    def inverse_transform(self, X):
        return (X * self.iqr) + self.median


def denormalize(X_normalized, normalization_info):
    X_denormalized = X_normalized.clone()

    for var, info in normalization_info.items():
        if info["method"] == "standardization":
            mean = info["mean"]
            std = info["std"]
            X_denormalized[:, :, var] = (
                X_denormalized[:, :, var] * std) + mean
        if info["method"] == "log_standardization":
            mean = info["mean"]
            std = info["std"]
            X_denormalized[:, :, var] = torch.expm1(
                (X_denormalized[:, :, var] * std) + mean)
        elif info["method"] == "log_robust_scaling":
            scaler = info["scaler"]
            X_denormalized[:, :, var] = torch.expm1(
                scaler.inverse_transform(X_denormalized[:, :, var]))

    return X_denormalized


def reshape_node_features_unordered(x, batch, num_graphs):
    """
    Reshapes data.x from [batch_size * k, n] to [batch_size, k, n].
    Works even if nodes are not ordered by graph.
    """
    import torch

    batch_size = num_graphs  # Number of graphs in the batch
    num_node_features = x.size(1)

    # Calculate number of nodes per graph
    unique_batches, counts = torch.unique(batch, return_counts=True)
    num_nodes_per_graph = counts[0].item()  # Assuming all graphs have same number of nodes

    # Initialize the reshaped tensor
    x_reshaped = x.new_zeros((batch_size, num_nodes_per_graph, num_node_features))

    # Get indices for each graph
    for i in range(batch_size):
        mask = batch == i
        x_reshaped[i] = x[mask]
    
    return x_reshaped


def validation_step(model, dataloader, norm_info, epoch):
    """Validation Step"""
    model.eval()
    loss_epoch = 0

    all_outvars = []
    all_predvars = []

    with torch.no_grad():
        for data in dataloader:
            data = data.to(device)
            out = model(data.x, data.edge_index,
                        data.edge_attr, data.batch)  # Forward pass
            loss = F.mse_loss(out, data.y)
            loss_epoch += loss.item() * data.num_graphs  # Accumulate loss

            if epoch % 1 == 0:
                # Store outputs for later analysis
                all_outvars.append(reshape_node_features_unordered(data.y, data.batch, data.num_graphs))
                all_predvars.append(reshape_node_features_unordered(out, data.batch, data.num_graphs))

    loss_epoch /= len(dataloader.dataset)
    
    if epoch % 1 == 0:
        # Concatenate all predictions and ground truths
        all_outvars = torch.cat(all_outvars, dim=0).to(device)
        all_predvars = torch.cat(all_predvars, dim=0).to(device)
        print(dataloader.dataset.X.shape)
        # Convert data to numpy
        outvar = denormalize(all_outvars, norm_info[1]).detach().cpu().numpy()
        predvar = denormalize(
            all_predvars, norm_info[1]).detach().cpu().numpy()

        # Plotting
        fig, axs = plt.subplots(3, 2, figsize=(8, 12))

        output_names = ['n', 'v', 'T']
        lims = [[50, 500000000], [0, 800], [0, 5]]

        for i in range(3):
            axs[i, 0].plot(outvar[:, :, i].T)
            axs[i, 0].set_yscale('log') if i == 0 else None
            axs[i, 0].set_ylim(lims[i])
            axs[i, 1].plot(predvar[:, :, i].T)
            axs[i, 1].set_yscale('log') if i == 0 else None
            axs[i, 1].set_ylim(lims[i])

            axs[i, 0].set_title(f'{output_names[i]} true')
            axs[i, 1].set_title(f'{output_names[i]} predicted')

        fig.savefig(f"results_{epoch}.png")
        plt.close()

    return loss_epoch

@hydra.main(version_base="1.3", config_path="conf", config_name="config_gnn.yaml")
def main(cfg: DictConfig):

    LaunchLogger.initialize()

    # Use Diffusion equation for the Darcy PDE
    # darcy = Diffusion(T="u", time=False, dim=2, D="k", Q=1.0 * 4.49996e00 * 3.88433e-03)
    # darcy_node = darcy.make_nodes()
    X_train_normalized = torch.load(to_absolute_path(
        './datasets/multivp/X_train_normalized.pt')).swapaxes(1,2).to(torch.float32).to(device)
    y_train_normalized = torch.load(to_absolute_path(
        './datasets/multivp/y_train_normalized.pt')).swapaxes(1,2).to(torch.float32).to(device)
    X_val_normalized = torch.load(to_absolute_path(
        './datasets/multivp/X_val_normalized.pt')).swapaxes(1,2).to(torch.float32).to(device)
    y_val_normalized = torch.load(to_absolute_path(
        './datasets/multivp/y_val_normalized.pt')).swapaxes(1,2).to(torch.float32).to(device)
    # load normalization_info_inputs.pt
    normalization_info_inputs = torch.load(
        to_absolute_path('./datasets/multivp/normalization_info_inputs.pt'), map_location=device)
    normalization_info_outputs = torch.load(
        to_absolute_path('./datasets/multivp/normalization_info_outputs.pt'), map_location=device)

    norm_info = (normalization_info_inputs, normalization_info_outputs)

    dataset = ProfilesToGraphDataset(X_train_normalized, y_train_normalized)
    validation_dataset = ProfilesToGraphDataset(
        X_val_normalized, y_val_normalized)

    dataloader = DataLoader(dataset, batch_size=cfg.batch_size, shuffle=True)

    validation_dataloader = DataLoader(
        validation_dataset, batch_size=cfg.batch_size, shuffle=False)
    
    model = GNN(
        input_dim=cfg.model.gnn.input_dim,
        edge_dim=cfg.model.gnn.edge_dim,
        hidden_dim=cfg.model.gnn.hidden_dim,
        output_dim=cfg.model.gnn.output_dim,
        num_layers=cfg.model.gnn.num_layers,
    ).to(device)

    optimizer = torch.optim.AdamW(
        model.parameters(),
        betas=cfg.optimizer_params.betas,
        lr=cfg.optimizer_params.lr,
        weight_decay=cfg.optimizer_params.weight_decay,
    )

    scheduler = torch.optim.lr_scheduler.ExponentialLR(
        optimizer, gamma=cfg.optimizer_params.gamma)

    for epoch in range(cfg.max_epochs):
        # wrap epoch in launch logger for console logs
        with LaunchLogger(
            "train",
            epoch=epoch,
            num_mini_batch=len(dataloader),
            epoch_alert_freq=10,
        ) as log:
            for data in dataloader:
                optimizer.zero_grad()
                data = data.to(device)
                # invar = data[0]
                # outvar = data[1]
                # print(invar.shape, outvar.shape, invar[:, 0].unsqueeze(dim=1).shape)
                # Compute forward pass
                out = model(data.x, data.edge_index, data.edge_attr, data.batch)
                """dxf = 1.0 / out.shape[-2]
                dyf = 1.0 / out.shape[-1]

                # Compute gradients using finite difference
                sol_x = dx(out, dx=dxf, channel=0, dim=1, order=1, padding="zeros")
                sol_y = dx(out, dx=dyf, channel=0, dim=0, order=1, padding="zeros")
                sol_x_x = ddx(out, dx=dxf, channel=0, dim=1, order=1, padding="zeros")
                sol_y_y = ddx(out, dx=dyf, channel=0, dim=0, order=1, padding="zeros")

                k_x = dx(invar, dx=dxf, channel=0, dim=1, order=1, padding="zeros")
                k_y = dx(invar, dx=dxf, channel=0, dim=0, order=1, padding="zeros")

                k, _, _ = (
                    invar[:, 0],
                    invar[:, 1],
                    invar[:, 2],
                )

                pde_out = darcy_node[0].evaluate(
                    {
                        "u__x": sol_x,
                        "u__y": sol_y,
                        "u__x__x": sol_x_x,
                        "u__y__y": sol_y_y,
                        "k": k,
                        "k__x": k_x,
                        "k__y": k_y,
                    }
                )

                pde_out_arr = pde_out["diffusion_u"]
                pde_out_arr = F.pad(
                    pde_out_arr[:, :, 2:-2, 2:-2], [2, 2, 2, 2], "constant", 0
                )
                loss_pde = F.l1_loss(pde_out_arr, torch.zeros_like(pde_out_arr))"""

                # Compute data loss
                loss_data = F.mse_loss(data.y, out)

                # Compute total loss
                loss = loss_data  # + 1 / 240 * cfg.phy_wt * loss_pde

                # Backward pass and optimizer and learning rate update
                loss.backward()
                optimizer.step()
                log.log_minibatch(
                    # , "loss_pde": loss_pde.detach()}
                    {"loss_data": loss_data.detach()}
                )

            log.log_epoch({"Learning Rate": optimizer.param_groups[0]["lr"]})
            scheduler.step()

        with LaunchLogger("valid", epoch=epoch) as log:
            error = validation_step(
                model, validation_dataloader, norm_info, epoch)
            log.log_epoch({"Validation error": error})
            pass

        save_checkpoint(
            "./checkpoints",
            models=model,
            optimizer=optimizer,
            scheduler=scheduler,
            epoch=epoch,
        )


if __name__ == "__main__":
    main()

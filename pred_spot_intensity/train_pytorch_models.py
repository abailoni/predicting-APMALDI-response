from pathlib import Path

import pandas as pd
import numpy as np
import sklearn
import tqdm
from sklearn.cluster import KMeans

try:
    import torch.nn.functional as F
    import pytorch_lightning as pl
    import torch.nn as nn
    import torch
    from pytorch_lightning.callbacks import LearningRateMonitor
    from torch.utils.data import DataLoader
    from pred_spot_intensity.pytorch_utils import TrainValData, TestData, SorensenDiceLoss, SimpleTwoLayersNN
    import shap
except ImportError:
    torch = None
import scipy.cluster.hierarchy

try:
    from allrank.models.losses.neuralNDCG import neuralNDCG, neuralNDCG_transposed
except ImportError:
    neuralNDCG = None

# TODO: give as argument
DEVICE = "cpu"


class NeuralNDCGLoss:
    """
    Simple class wrapper of neuralNDCG loss
    """

    def __init__(self, **loss_kwargs):
        assert neuralNDCG is not None, "allRnak package is required"
        self.loss_kwargs = loss_kwargs

    def __call__(self, pred, gt):
        return neuralNDCG(pred, gt, **self.loss_kwargs)


def train_torch_model(model, train_loader, val_loader=None, test_loader=None,
                      max_epochs=1000):
    # Train model using Lightning:
    lr_monitor = LearningRateMonitor(logging_interval='epoch')

    trainer = pl.Trainer(
        callbacks=[lr_monitor],
        default_root_dir=str(Path.cwd() / "training_data_torch"),
        gradient_clip_algorithm="norm",
        enable_progress_bar=True,
        max_epochs=max_epochs,
        detect_anomaly=True,
        log_every_n_steps=5,
        accelerator=DEVICE,
        # gpus=0,
        #    auto_lr_find=True,
        # ckpt_path="path",
    )
    trainer.fit(model=model,
                train_dataloaders=train_loader,
                val_dataloaders=val_loader)

    if test_loader is not None:
        # Predict and save predictions on test set:
        prediction = trainer.predict(model, dataloaders=test_loader)
        # nb_batches = len(prediction)
        pred_array = np.concatenate([tensor[0].numpy() for tensor in prediction])
        pred_indices = np.concatenate([tensor[1].numpy() for tensor in prediction])
        return trainer, pred_array, pred_indices
    else:
        return trainer


def train_torch_model_cross_val_loop(X, Y, task_name,
                                     stratification_classes,
                                     ignore_mask=None,
                                     max_epochs=1000,
                                     batch_size=32,
                                     learning_rate=0.001,
                                     num_cross_val_folds=10,
                                     num_hidden_layer_features=32
                                     ):
    """
    TODO: Refactor arguments: remove task name and add final_activation/loss as arguments
    """
    # Initial definitions:
    ignore_mask = ignore_mask if ignore_mask is None else ignore_mask.astype("float32")
    train_val_dataset = TrainValData(X.astype("float32"), Y.astype("float32"),
                                     ignore_mask=ignore_mask)
    test_data = TestData(X.astype("float32"))
    all_results = pd.DataFrame()
    if task_name == "ranking":
        final_activation = None
        loss_function = NeuralNDCGLoss()
    elif task_name == "detection":
        final_activation = nn.Sigmoid()
        loss_function = SorensenDiceLoss()
        # soresen_loss = F.binary_cross_entropy_with_logits
    else:
        raise ValueError(task_name)

    # Define cross-val split:
    skf = sklearn.model_selection.StratifiedKFold(n_splits=num_cross_val_folds)
    skf.get_n_splits()
    pbar_cross_split = tqdm.tqdm(skf.split(range(X.shape[0]), stratification_classes),
                                 leave=False, total=num_cross_val_folds)

    # Loop over cross-val folds:
    for fold, (train_index, test_index) in enumerate(pbar_cross_split):
        # Define data-loaders:
        train_sampler = torch.utils.data.SubsetRandomSampler(train_index.tolist())
        valid_sampler = torch.utils.data.SubsetRandomSampler(test_index.tolist())
        num_workers = 0
        train_loader = DataLoader(dataset=train_val_dataset, batch_size=batch_size, sampler=train_sampler,
                                  num_workers=num_workers, drop_last=True)
        val_loader = DataLoader(dataset=train_val_dataset, batch_size=batch_size, sampler=valid_sampler,
                                num_workers=num_workers)
        test_loader = DataLoader(dataset=test_data, batch_size=batch_size, sampler=valid_sampler,
                                 num_workers=num_workers)

        # Define model:
        model = SimpleTwoLayersNN(num_feat=num_hidden_layer_features,
                                  nb_in_feat=X.shape[1],
                                  nb_out_feat=Y.shape[1],
                                  loss=loss_function,
                                  learning_rate=learning_rate,
                                  final_activation=final_activation,
                                  has_ignore_mask=task_name=="detection")

        _, pred_array, pred_indices = train_torch_model(model, train_loader, val_loader, test_loader, max_epochs)

        lc_results = pd.DataFrame(pred_array, index=pred_indices)
        lc_results["fold"] = fold
        all_results = pd.concat([all_results, lc_results])

    return all_results



def features_selection_torch_model(X, Y, task_name,
                                     ignore_mask=None,
                                     max_epochs=1000,
                                     batch_size=32,
                                     learning_rate=0.001,
                                     num_cross_val_folds=10,
                                     num_hidden_layer_features=32,
                                     checkpoint_path=None,
                                     only_train=False
                                     ):
    """
    TODO: Refactor arguments: remove task name and add final_activation/loss as arguments
    """
    # Initial definitions:
    ignore_mask = ignore_mask if ignore_mask is None else ignore_mask.astype("float32")
    train_val_dataset = TrainValData(X.astype("float32"), Y.astype("float32"),
                                     ignore_mask=ignore_mask)
    test_data = TestData(X.astype("float32"))
    all_results = pd.DataFrame()
    if task_name == "ranking":
        final_activation = None
        loss_function = NeuralNDCGLoss()
    elif task_name == "detection":
        final_activation = nn.Sigmoid()
        loss_function = SorensenDiceLoss()
        # soresen_loss = F.binary_cross_entropy_with_logits
    elif task_name == "regression":
        final_activation = None
        loss_function = nn.MSELoss()
        # loss_function = nn.L1Loss() # Does not work
    else:
        raise ValueError(task_name)

    # Define data-loaders:
    num_workers = 0
    train_loader = DataLoader(dataset=train_val_dataset, batch_size=batch_size,
                              num_workers=num_workers, drop_last=True)

    # Define model:
    if checkpoint_path is None:
        model = SimpleTwoLayersNN(num_feat=num_hidden_layer_features,
                                  nb_in_feat=X.shape[1],
                                  nb_out_feat=Y.shape[1],
                                  loss=loss_function,
                                  learning_rate=learning_rate,
                                  final_activation=final_activation,
                                  has_ignore_mask=task_name=="detection")

        trainer = train_torch_model(model, train_loader, max_epochs=max_epochs) # TODO: epochs
        # model = trainer.model
        from torchmetrics import SpearmanCorrCoef
        spearman = SpearmanCorrCoef()

        test_loader = DataLoader(dataset=test_data, batch_size=batch_size,
                                 num_workers=num_workers)
        predictions = trainer.predict(model, dataloaders=test_loader)
        ignore_mask_flatten = ignore_mask.flatten().astype("bool")
        pred_array = np.concatenate([tensor[0].numpy() for tensor in predictions]).astype("float32").flatten()[~ignore_mask_flatten]
        print("Score MSE: ", scipy.stats.spearmanr(pred_array, Y.astype("float32").flatten()[~ignore_mask_flatten]))
    else:
        model = SimpleTwoLayersNN.load_from_checkpoint(checkpoint_path)


    e = shap.DeepExplainer(
        model,
        torch.from_numpy(X[np.random.choice(np.arange(X.shape[0]), 100, replace=False)]
                         ).to(DEVICE).float())
    shap_values = e.shap_values(
        torch.from_numpy(X).to(DEVICE).float()
    )

    return shap_values



def train_pytorch_model_on_intensities(intensities_df,
                                       features_df,
                                       adducts_one_hot,
                                       task_name,
                                       do_feature_selection=False,
                                       path_feature_importance_csv=None,
                                       num_cross_val_folds=10,
                                       intensity_column="norm_intensity",
                                       checkpoint_path=None,
                                       use_adduct_features=True,
                                       adducts_columns=None
                                       ):
    assert torch is not None

    # -------------------------
    # Reshape data to training format:
    # -------------------------
    index_cols = ['name_short', 'adduct'] if use_adduct_features else ['name_short']
    Y = intensities_df.pivot(index=index_cols, columns=["matrix", "polarity"], values=intensity_column)
    Y_detected = intensities_df.pivot(index=index_cols, columns=["matrix", "polarity"], values="detected")

    # Mask NaN values (ion-intensity32 values not provided for a given matrix-polarity):
    Y_is_na = Y_detected.isna()
    Y_detected[Y_is_na] = False

    detected_ion_mask = Y_detected.sum(axis=1) > 0
    if task_name == "ranking":
        # Remove ions that are never detected:
        Y = Y[detected_ion_mask]
        Y_detected = Y_detected[detected_ion_mask]
        Y_is_na = Y_is_na[detected_ion_mask]

    # Set not-detected intensities to zero:
    Y[Y_detected == False] = 0

    # Get feature array used for training:
    X = pd.DataFrame(features_df.loc[Y.index.get_level_values(0)].to_numpy(), index=Y.index)
    if use_adduct_features:
        X = X.join(pd.DataFrame(adducts_one_hot.loc[Y.index.get_level_values(1)].to_numpy(), index=Y.index),
               how="inner", rsuffix="adduct")

    # -------------------------
    # Find stratification classes and start training:
    # -------------------------
    # # TODO: use alternative stratification
    # Z_clust = scipy.cluster.hierarchy.linkage(Y, method="ward")
    # out_clustering = scipy.cluster.hierarchy.fcluster(Z_clust, t=9, criterion="distance")

    kmeans = KMeans(n_clusters=10, random_state=45).fit(Y)
    out_clustering = kmeans.labels_

    # # Sorting should not be necessary...
    # masked_Y = np.ma.masked_array(Y.to_numpy(), mask=Y_detected.to_numpy())
    # masked_Y.argsort(axis=1, fill_value=-1)
    # np.ma.argsort()
    # np.argsort(Y.to_numpy(), axis=1, )

    ignore_mask = None
    if task_name == "ranking":
        # Not-detected intensities are masked to value -1 and will be ignored in the ranking loss:
        Y[Y_is_na] = -1
    elif task_name == "detection":
        Y = Y_detected
        ignore_mask = Y_is_na.to_numpy()
    elif task_name == "regression":
        # Mask not-detected ones:
        ignore_mask = np.logical_not(Y_detected.to_numpy())
    else:
        raise ValueError

    # -------------------------
    # Train:
    # -------------------------
    if not do_feature_selection:
        out = train_torch_model_cross_val_loop(X.to_numpy(), Y.to_numpy(),
                                           task_name,
                                           stratification_classes=out_clustering,
                                           ignore_mask=ignore_mask,
                                           num_cross_val_folds=num_cross_val_folds,
                                           max_epochs=20,
                                           # max_epochs=1,
                                           batch_size=8,
                                           # batch_size=32,
                                           learning_rate=0.01,
                                           num_hidden_layer_features=32)
        # -------------------------
        # Reshape results:
        # -------------------------
        matrix_multi_index = Y.columns
        training_results = out.sort_index()
        # Set index with molecule/adduct names:
        training_results = pd.DataFrame(training_results.to_numpy(), index=Y.index, columns=training_results.columns)
        reshaped_gt = Y.stack([i for i in range(len(matrix_multi_index.levels))], )
        reshaped_gt.name = "observed_value"
        reshaped_prediction = pd.DataFrame(training_results.drop(columns="fold").to_numpy(), index=Y.index,
                                           columns=matrix_multi_index).stack(
            [i for i in range(len(matrix_multi_index.levels))])
        reshaped_prediction.name = "prediction"
        reshaped_out = reshaped_gt.to_frame().join(reshaped_prediction.to_frame(), how="inner")

        # Add back fold info:
        reshaped_out["fold"] = training_results.loc[[i for i in zip(reshaped_out.index.get_level_values(0),
                                                                    reshaped_out.index.get_level_values(
                                                                        1))], "fold"].to_numpy()
        reshaped_out["model_type"] = "NN"
        reshaped_out.reset_index(inplace=True)

        return reshaped_out

    else:
        shap_values = features_selection_torch_model(X.to_numpy(), Y.to_numpy(),
                                               task_name,
                                               ignore_mask=ignore_mask,
                                               num_cross_val_folds=num_cross_val_folds,
                                               max_epochs=100, # TODO: change
                                               # max_epochs=1,
                                               batch_size=8,
                                               # batch_size=32,
                                               learning_rate=0.01,
                                               num_hidden_layer_features=128,
                                             checkpoint_path=checkpoint_path)
        print("done")

        feat_names = features_df.columns.tolist()
        if use_adduct_features:
            feat_names = adducts_columns.tolist() + feat_names

        matrix_multi_index = Y.columns
        ignore_mask = ignore_mask.astype("bool")
        # Filter out ignored items:
        # filtered_shap_values = [shap_values[matr_idx][ignore_mask[:,matr_idx]] for matr_idx in range(matrix_multi_index.shape[0])]
        # TODO: ignore stuff in the mask
        # df = pd.DataFrame(columns=["mean_abs_shap", "matrix", "polarity", "feat_name"])
        df_feat_importance = pd.DataFrame()
        for i, (matrix, polarity) in enumerate(matrix_multi_index):
            loc_shap_values = shap_values[i][~ignore_mask[:, i]]
            loc_df = pd.DataFrame({
                "mean_abs_shap": np.mean(np.abs(loc_shap_values), axis=0),
                "matrix": matrix,
                "polarity": polarity,
                "feature_name": feat_names
            })
            df_feat_importance = pd.concat([df_feat_importance, loc_df])
        df_feat_importance = df_feat_importance.sort_values("mean_abs_shap", ascending=False).set_index("feature_name", drop=True)
        return df_feat_importance


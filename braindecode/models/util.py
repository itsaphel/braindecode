# Authors: Robin Schirrmeister <robintibor@gmail.com>
#          Hubert Banville <hubert.jbanville@gmail.com>
#
# License: BSD (3-clause)

import torch
from torch import nn
import numpy as np
from scipy.special import log_softmax


def to_dense_prediction_model(model, axis=(2, 3)):
    """
    Transform a sequential model with strides to a model that outputs
    dense predictions by removing the strides and instead inserting dilations.
    Modifies model in-place.

    Parameters
    ----------
    model: torch.nn.Module
        Model which modules will be modified
    axis: int or (int,int)
        Axis to transform (in terms of intermediate output axes)
        can either be 2, 3, or (2,3).

    Notes
    -----
    Does not yet work correctly for average pooling.
    Prior to version 0.1.7, there had been a bug that could move strides
    backwards one layer.

    """
    if not hasattr(axis, "__len__"):
        axis = [axis]
    assert all([ax in [2, 3] for ax in axis]), "Only 2 and 3 allowed for axis"
    axis = np.array(axis) - 2
    stride_so_far = np.array([1, 1])
    for module in model.modules():
        if hasattr(module, "dilation"):
            assert module.dilation == 1 or (module.dilation == (1, 1)), (
                "Dilation should equal 1 before conversion, maybe the model is "
                "already converted?"
            )
            new_dilation = [1, 1]
            for ax in axis:
                new_dilation[ax] = int(stride_so_far[ax])
            module.dilation = tuple(new_dilation)
        if hasattr(module, "stride"):
            if not hasattr(module.stride, "__len__"):
                module.stride = (module.stride, module.stride)
            stride_so_far *= np.array(module.stride)
            new_stride = list(module.stride)
            for ax in axis:
                new_stride[ax] = 1
            module.stride = tuple(new_stride)


def get_output_shape(model, in_chans, input_window_samples):
    """Returns shape of neural network output for batch size equal 1.

    Returns
    -------
    output_shape: tuple
        shape of the network output for `batch_size==1` (1, ...)
    """
    with torch.no_grad():
        dummy_input = torch.ones(
            1, in_chans, input_window_samples,
            dtype=next(model.parameters()).dtype,
            device=next(model.parameters()).device,
        )
        output_shape = model(dummy_input).shape
    return output_shape


def _pad_shift_array(x, stride=1):
    """Zero-pad and shift rows of a 3D array.

    E.g., used to align predictions of corresponding windows in
    sequence-to-sequence models.

    Parameters
    ----------
    x : np.ndarray
        Array of shape (n_rows, n_classes, n_windows).
    stride : int
        Number of non-overlapping elements between two consecutive sequences.

    Returns
    -------
    np.ndarray :
        Array of shape (n_rows, n_classes, (n_rows - 1) * stride + n_windows)
        where each row is obtained by zero-padding the corresponding row in
        ``x`` before and after in the last dimension.
    """
    if x.ndim != 3:
        raise NotImplementedError(
            'x must be of shape (n_rows, n_classes, n_windows), got '
            f'{x.shape}')
    x_padded = np.pad(x, ((0, 0), (0, 0), (0, (x.shape[0] - 1) * stride)))
    orig_strides = x_padded.strides
    new_strides = (orig_strides[0] - stride * orig_strides[2],
                   orig_strides[1],
                   orig_strides[2])
    return np.lib.stride_tricks.as_strided(x_padded, strides=new_strides)


def aggregate_probas(logits, n_windows_stride=1):
    """Aggregate predicted probabilities with self-ensembling.

    Aggregate window-wise predicted probabilities obtained on overlapping
    sequences of windows using multiplicative voting as described in
    [Phan2018]_.

    Parameters
    ----------
    logits : np.ndarray
        Array of shape (n_sequences, n_classes, n_windows) containing the
        logits (i.e. the raw unnormalized scores for each class) for each
        window of each sequence.
    n_windows_stride : int
        Number of windows between two consecutive sequences. Default is 1
        (maximally overlapping sequences).

    Returns
    -------
    np.ndarray :
        Array of shape ((n_rows - 1) * stride + n_windows, n_classes)
        containing the aggregated predicted probabilities for each window
        contained in the input sequences.

    References
    ----------
    .. [Phan2018] Phan, H., Andreotti, F., Cooray, N., Chén, O. Y., &
        De Vos, M. (2018). Joint classification and prediction CNN framework
        for automatic sleep stage classification. IEEE Transactions on
        Biomedical Engineering, 66(5), 1285-1296.
    """
    log_probas = log_softmax(logits, axis=1)
    return _pad_shift_array(log_probas, stride=n_windows_stride).sum(axis=0).T


class TimeDistributed(nn.Module):
    """Extract features for multiple windows, concatenate then classify them.

    Extract features from a sequence of windows using a provided feature
    extractor, then concatenate the features and pass them to a classifier (by
    default, a linear layer with dropout).
    Useful when training a sequence-to-prediction model (e.g. sleep stager
    which must map a sequence of consecutive windows to the label of the middle
    window in the sequence).

    Parameters
    ----------
    feat_extractor : nn.Module
        Model that extracts features from input arrays. The output of
        ``feat_extractor`` will be flattened into a 1D array.
    feat_size : int | None
        Number of elements in the output of ``feat_extractor``. Ignored if
        ``clf`` is provided.
    n_windows : int | None
        Number of windows whose features must be concatenated. Ignored if
        ``clf`` is provided.
    n_classes : int | None
        Number of classes. Ignored if ``clf`` is provided.
    dropout : float
        Dropout to be applied before the linear layer. Ignored if ``clf`` is
        provided.
    clf : nn.Module | None
        If provided, module that will receive the concatenated features and
        produce a prediction. If None, a simple linear layer with dropout
        is used, as defined using parameters ``feat_size``, ``n_windows``,
        ``n_classes`` and ``dropout``.
    """
    def __init__(self, feat_extractor, feat_size=None, n_windows=None,
                 n_classes=None, dropout=0.25, clf=None):
        super().__init__()
        self.feat_extractor = feat_extractor
        if clf is None:
            self.clf = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(feat_size * n_windows, n_classes)
            )
        else:
            self.clf = clf

    def forward(self, x):
        """
        Parameters
        ----------
        x : torch.Tensor
            Sequence of windows, of shape (batch_size, seq_len, n_channels,
            n_times).
        """
        feats = [self.feat_extractor(x[:, i]) for i in range(x.shape[1])]
        feats = torch.stack(feats, dim=1).flatten(start_dim=1)
        return self.clf(feats)

import torch
from torch import nn, Tensor
from torch.nn import functional as F

import numpy as np
import timm


class CrossEntropy(nn.Module):
    def __init__(self, ignore_label: int = 255, weight: Tensor = None, aux_weights: list = [1, 0.4, 0.4]) -> None:
        super().__init__()
        self.aux_weights = aux_weights
        self.criterion = nn.CrossEntropyLoss(weight=weight, ignore_index=ignore_label)

    def _forward(self, preds: Tensor, labels: Tensor) -> Tensor:
        # preds in shape [B, C, H, W] and labels in shape [B, H, W]
        return self.criterion(preds, labels)

    def forward(self, preds, labels: Tensor) -> Tensor:
        if isinstance(preds, tuple):
            return sum([w * self._forward(pred, labels) for (pred, w) in zip(preds, self.aux_weights)])
        return self._forward(preds, labels)


class OhemCrossEntropy(nn.Module):
    def __init__(self, ignore_label: int = 255, weight: Tensor = None, thresh: float = 0.7, aux_weights: list = [1, 1]) -> None:
        super().__init__()
        self.ignore_label = ignore_label
        self.aux_weights = aux_weights
        self.thresh = -torch.log(torch.tensor(thresh, dtype=torch.float))
        self.criterion = nn.CrossEntropyLoss(weight=weight, ignore_index=ignore_label, reduction='none')

    def _forward(self, preds: Tensor, labels: Tensor) -> Tensor:
        # preds in shape [B, C, H, W] and labels in shape [B, H, W]
        n_min = labels[labels != self.ignore_label].numel() // 16
        loss = self.criterion(preds, labels).view(-1)
        loss_hard = loss[loss > self.thresh]

        if loss_hard.numel() < n_min:
            loss_hard, _ = loss.topk(n_min)

        return torch.mean(loss_hard)

    def forward(self, preds, labels: Tensor) -> Tensor:
        if isinstance(preds, tuple):
            return sum([w * self._forward(pred, labels) for (pred, w) in zip(preds, self.aux_weights)])
        return self._forward(preds, labels)


class DiceLoss(nn.Module):
    def __init__(self, epsilon=1e-5):
        super(DiceLoss, self).__init__()
        self.epsilon = epsilon

    def forward(self, pred, label):
        shape = pred.shape
        assert pred.shape[-1] == label.shape[-1] and pred.shape[-2] == label.shape[-2]
        total_loss = 0.

        label = F.one_hot(label, num_classes=shape[1]).permute(0, 3, 1, 2).long()
        for i in range(shape[1]):
            top = 2 * torch.sum(torch.mul(pred[:,i], label[:,i]), dtype=float)

            bottom = torch.sum(pred[:,i], dtype=float) + torch.sum(label[:,i], dtype=float)
            bottom = torch.max(bottom, (torch.ones_like(bottom, dtype=float) * self.epsilon))
            loss_tmp = -1 * (top / bottom)
            total_loss += loss_tmp
            # print(top.item(), bottom.item(), loss_tmp.item())
            # print()
        # print(total_loss.item())
        return total_loss


class SegmentationLoss(object):
    def __init__(self, weight=None, size_average=True, batch_average=False, ignore_index=255, cuda=False):
        self.ignore_index = ignore_index
        self.weight = weight
        self.size_average = size_average
        self.batch_average = batch_average
        self.cuda = cuda

    def build_loss(self, mode='ce'):
        """Choices: ['ce' or 'focal']"""
        if mode == 'ce':
            return self.CrossEntropyLoss
        elif mode == 'focal':
            return self.FocalLoss
        else:
            raise NotImplementedError

    def CrossEntropyLoss(self, logit, target):
        n, c, h, w = logit.shape
        criterion = nn.CrossEntropyLoss(
            weight=self.weight, ignore_index=self.ignore_index,
            size_average=self.size_average
        )
        if self.cuda:
            criterion = criterion.to("cuda")

        loss = criterion(logit, target)

        if self.batch_average:
            loss /= n

        return loss

    def FocalLoss(self, logit, target, gamma=2, alpha=0.5):
        n, c, h, w = logit.size()
        criterion = nn.CrossEntropyLoss(
            weight=self.weight, ignore_index=self.ignore_index,
            size_average=self.size_average
        )
        if self.cuda:
            criterion = criterion.to("cuda")

        logpt = - criterion(logit, target)
        pt = torch.exp(logpt)
        if alpha is not None:
            logpt *= alpha
        loss = -((1 - pt) ** gamma) * logpt

        if self.batch_average:
            loss /= n

        return loss


def compute_compound_loss(
    criterion_dict: dict,
    raw_network_outputs: torch.Tensor,
    label: torch.Tensor,
    blob_loss_mode=False,
    masked=True,
):
    """
    This computes a compound loss by looping through a criterion dict!
    """
    # vprint("outputs:", outputs)
    losses = []
    for entry in criterion_dict.values():
        # name = entry["name"]
        criterion = entry["loss"]
        weight = entry["weight"]

        sigmoid = entry["sigmoid"]
        if blob_loss_mode == False:
            if sigmoid == True:
                sigmoid_network_outputs = torch.sigmoid(raw_network_outputs)
                individual_loss = criterion(sigmoid_network_outputs, label)
            else:
                individual_loss = criterion(raw_network_outputs, label)
        elif blob_loss_mode == True:
            if masked == True:  # this is the default blob loss
                if sigmoid == True:
                    sigmoid_network_outputs = torch.sigmoid(raw_network_outputs)
                    individual_loss = compute_blob_loss_multi(
                        criterion=criterion,
                        network_outputs=sigmoid_network_outputs,
                        multi_label=label,
                    )
                else:
                    individual_loss = compute_blob_loss_multi(
                        criterion=criterion,
                        network_outputs=raw_network_outputs,
                        multi_label=label,
                    )
            elif masked == False:  # without masking for ablation study
                if sigmoid == True:
                    sigmoid_network_outputs = torch.sigmoid(raw_network_outputs)
                    individual_loss = compute_no_masking_multi(
                        criterion=criterion,
                        network_outputs=sigmoid_network_outputs,
                        multi_label=label,
                    )
                else:
                    individual_loss = compute_no_masking_multi(
                        criterion=criterion,
                        network_outputs=raw_network_outputs,
                        multi_label=label,
                    )

        weighted_loss = individual_loss * weight
        losses.append(weighted_loss)

    loss = sum(losses)
    return loss


def compute_blob_loss_multi(
    criterion,
    network_outputs: torch.Tensor,
    multi_label: torch.Tensor,
):
    """
    1. loop through elements in our batch
    2. loop through blobs per element compute loss and divide by blobs to have element loss
    2.1 we need to account for sigmoid and non/sigmoid in conjunction with BCE
    3. divide by batch length to have a correct batch loss for back prop
    """
    batch_length = multi_label.shape[0]

    element_blob_loss = []
    # loop over elements
    for element in range(batch_length):
        if element < batch_length:
            end_index = element + 1
        elif element == batch_length:
            end_index = None

        element_label = multi_label[element:end_index, ...]

        element_output = network_outputs[element:end_index, ...]

        # loop through labels
        unique_labels = torch.unique(element_label)
        # blob_count = len(unique_labels) - 1

        label_loss = []
        for ula in unique_labels:
            if ula == 0:
                pass
            else:
                # first we need one hot labels
                label_mask = element_label > 0
                # we flip labels
                label_mask = ~label_mask

                # we set the mask to true where our label of interest is located
                # vprint(torch.count_nonzero(label_mask))
                label_mask[element_label == ula] = 1
                # vprint(torch.count_nonzero(label_mask))
                # vprint("torch.unique(label_mask):", torch.unique(label_mask))

                the_label = element_label == ula

                # debugging
                # masked_label = the_label * label_mask
                # vprint("masked_label:", torch.count_nonzero(masked_label))

                masked_output = element_output * label_mask

                try:
                    # we try with int labels first, but some losses require floats
                    blob_loss = criterion(masked_output, the_label.int())
                except:
                    # if int does not work we try float
                    blob_loss = criterion(masked_output, the_label.long())

                label_loss.append(blob_loss)

        # compute mean
        # mean_label_loss = 0
        if not len(label_loss) == 0:
            mean_label_loss = sum(label_loss) / len(label_loss)
            # mean_label_loss = sum(label_loss) / \
            #     torch.count_nonzero(label_loss)
            element_blob_loss.append(mean_label_loss)

    # compute mean
    mean_element_blob_loss = 0
    if not len(element_blob_loss) == 0:
        mean_element_blob_loss = sum(element_blob_loss) / len(element_blob_loss)
        # element_blob_loss) / torch.count_nonzero(element_blob_loss)

    return mean_element_blob_loss


def compute_no_masking_multi(
    criterion,
    network_outputs: torch.Tensor,
    multi_label: torch.Tensor,
):
    """
    1. loop through elements in our batch
    2. loop through blobs per element compute loss and divide by blobs to have element loss
    2.1 we need to account for sigmoid and non/sigmoid in conjunction with BCE
    3. divide by batch length to have a correct batch loss for back prop
    """
    batch_length = multi_label.shape[0]

    element_blob_loss = []
    # loop over elements
    for element in range(batch_length):
        if element < batch_length:
            end_index = element + 1
        elif element == batch_length:
            end_index = None

        element_label = multi_label[element:end_index, ...]

        element_output = network_outputs[element:end_index, ...]

        # loop through labels
        unique_labels = torch.unique(element_label)
        blob_count = len(unique_labels) - 1

        label_loss = []
        for ula in unique_labels:
            if ula == 0:
                pass
            else:
                # first we need one hot labels

                the_label = element_label == ula

                # we compute the loss with no mask
                try:
                    # we try with int labels first, but some losses require floats
                    blob_loss = criterion(element_output, the_label.int())
                except:
                    # if int does not work we try float
                    blob_loss = criterion(element_output, the_label.long())

                label_loss.append(blob_loss)

            # compute mean
            # mean_label_loss = 0
            if not len(label_loss) == 0:
                mean_label_loss = sum(label_loss) / len(label_loss)
                # mean_label_loss = sum(label_loss) / \
                #     torch.count_nonzero(label_loss)
                element_blob_loss.append(mean_label_loss)

    # compute mean
    mean_element_blob_loss = 0
    if not len(element_blob_loss) == 0:
        mean_element_blob_loss = sum(element_blob_loss) / len(element_blob_loss)
        # element_blob_loss) / torch.count_nonzero(element_blob_loss)

    return mean_element_blob_loss


def compute_loss(
    blob_loss_dict: dict,
    criterion_dict: dict,
    blob_criterion_dict: dict,
    raw_network_outputs: torch.Tensor,
    binary_label: torch.Tensor,
    multi_label: torch.Tensor,
):
    """
    This function computes the total loss. It has a global main loss and the blob loss term which is computed separately for each connected component. The binary_label is the binarized label for the global part. The multi label features separate integer labels for each connected component.
    Example inputs should look like:
    blob_loss_dict = {
        "main_weight": 1,
        "blob_weight": 0,
    }
    criterion_dict = {
        "bce": {
            "name": "bce",
            "loss": BCEWithLogitsLoss(reduction="mean"),
            "weight": 1.0,
            "sigmoid": False,
        },
        "dice": {
            "name": "dice",
            "loss": DiceLoss(
                include_background=True,
                to_onehot_y=False,
                sigmoid=True,
                softmax=False,
                squared_pred=False,
            ),
            "weight": 1.0,
            "sigmoid": False,
        },
    }
    blob_criterion_dict = {
        "bce": {
            "name": "bce",
            "loss": BCEWithLogitsLoss(reduction="mean"),
            "weight": 1.0,
            "sigmoid": False,
        },
        "dice": {
            "name": "dice",
            "loss": DiceLoss(
                include_background=True,
                to_onehot_y=False,
                sigmoid=True,
                softmax=False,
                squared_pred=False,
            ),
            "weight": 1.0,
            "sigmoid": False,
        },
    }
    """

    main_weight = blob_loss_dict["main_weight"]
    blob_weight = blob_loss_dict["blob_weight"]

    # main loss
    # print(raw_network_outputs.shape, binary_label.shape)
    if main_weight > 0:
        main_loss = compute_compound_loss(
            criterion_dict=criterion_dict,
            raw_network_outputs=raw_network_outputs,
            label=binary_label,
            blob_loss_mode=False,
        )

    if blob_weight > 0:
        blob_loss = compute_compound_loss(
            criterion_dict=blob_criterion_dict,
            raw_network_outputs=raw_network_outputs,
            label=multi_label,
            blob_loss_mode=True,
        )

    # final loss
    if blob_weight == 0 and main_weight > 0:
        loss = main_loss
        blob_loss = 0

    elif main_weight == 0 and blob_weight > 0:
        loss = blob_loss
        main_loss = 0  # we set this to 0

    elif main_weight > 0 and blob_weight > 0:
        # print(main_loss.item(), blob_loss.item())
        loss = main_loss * main_weight + blob_loss * blob_weight

    return loss, main_loss, blob_loss

if __name__ == '__main__':
    pred = torch.randint(0, 19, (2, 19, 480, 640), dtype=torch.float)
    label = torch.randint(0, 19, (2, 480, 640), dtype=torch.long)
    loss_fn = compute_loss
    blob_loss_dict = {
        "main_weight": 2,
        "blob_weight": 1,
    }
    criterion_dict = {
        "ce": {
            "name": "ce",
            "loss": nn.CrossEntropyLoss(reduction="mean"),
            "weight": 1.0,
            "sigmoid": False,
        },
        "dice": {
            "name": "dice",
            "loss": DiceLoss(

            ),
            "weight": 1.0,
            "sigmoid": False,
        },
    }
    blob_criterion_dict = {
        "ce": {
            "name": "ce",
            "loss": nn.CrossEntropyLoss(reduction="mean"),
            "weight": 1.0,
            "sigmoid": False,
        },
        "dice": {
            "name": "dice",
            "loss": DiceLoss(

            ),
            "weight": 1.0,
            "sigmoid": False,
        },
    }
    y = loss_fn(blob_loss_dict, criterion_dict, blob_criterion_dict, pred, label, label)
    print(y)
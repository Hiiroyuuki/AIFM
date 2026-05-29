from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

from folderClassifier.encoder.transformer import FileStructureTransformer, PathStructureTokenizer


class FolderStructureDataset(Dataset):
    def __init__(self, data_root: str | Path, tokenizer: PathStructureTokenizer):
        self.data_root = Path(data_root).expanduser().resolve(strict=False)
        self.tokenizer = tokenizer
        self.folder_paths = self.collect_folder_paths()
        if not self.folder_paths:
            raise ValueError(f"No trainable folders found under: {self.data_root}")

    def collect_folder_paths(self) -> list[Path]:
        if not self.data_root.is_dir():
            raise NotADirectoryError(str(self.data_root))

        try:
            child_folders = [
                child.resolve(strict=False)
                for child in self.data_root.iterdir()
                if child.is_dir()
            ]
        except OSError as error:
            raise RuntimeError(f"Cannot read data root: {self.data_root}") from error

        return sorted(child_folders, key=lambda path: path.name.casefold()) or [self.data_root]

    def __len__(self) -> int:
        return len(self.folder_paths)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.tokenizer.tokenize_folder(self.folder_paths[index])


class MaskedFolderCollator:
    def __init__(
        self,
        tokenizer: PathStructureTokenizer,
        max_tokens: int,
        max_depth: int,
        mask_prob: float,
        mask_token_id: int,
    ):
        self.tokenizer = tokenizer
        self.max_tokens = max_tokens
        self.max_depth = max_depth
        self.mask_prob = mask_prob
        self.mask_token_id = mask_token_id

    def __call__(self, samples: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
        token_rows = []
        depth_rows = []
        type_rows = []
        mask_rows = []

        for sample in samples:
            token_ids = sample["token_ids"][: self.max_tokens]
            depth_ids = [
                min(depth, self.max_depth)
                for depth in sample["depth_ids"][: self.max_tokens]
            ]
            type_ids = sample["type_ids"][: self.max_tokens]
            valid_length = len(token_ids)
            pad_length = self.max_tokens - valid_length

            token_rows.append(token_ids + [PathStructureTokenizer.PAD_ID] * pad_length)
            depth_rows.append(depth_ids + [0] * pad_length)
            type_rows.append(type_ids + [PathStructureTokenizer.PAD_TYPE] * pad_length)
            mask_rows.append([1] * valid_length + [0] * pad_length)

        token_ids = torch.tensor(token_rows, dtype=torch.long)
        depth_ids = torch.tensor(depth_rows, dtype=torch.long)
        type_ids = torch.tensor(type_rows, dtype=torch.long)
        attention_mask = torch.tensor(mask_rows, dtype=torch.bool)
        input_token_ids, labels = self.apply_mlm_mask(token_ids, attention_mask)

        return {
            "input_token_ids": input_token_ids,
            "token_ids": token_ids,
            "depth_ids": depth_ids,
            "type_ids": type_ids,
            "attention_mask": attention_mask,
            "labels": labels,
        }

    def apply_mlm_mask(
        self,
        token_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        input_token_ids = token_ids.clone()
        labels = torch.full_like(token_ids, -100)
        maskable = attention_mask & (token_ids >= PathStructureTokenizer.HASH_OFFSET)
        random_mask = torch.rand(token_ids.shape) < self.mask_prob
        selected = maskable & random_mask

        for row_index in range(selected.shape[0]):
            if selected[row_index].any() or not maskable[row_index].any():
                continue
            candidates = torch.nonzero(maskable[row_index], as_tuple=False).flatten()
            chosen = candidates[torch.randint(len(candidates), (1,))]
            selected[row_index, chosen] = True

        labels[selected] = token_ids[selected]
        input_token_ids[selected] = self.mask_token_id
        return input_token_ids, labels


class MaskedFolderModel(nn.Module):
    def __init__(self, encoder: FileStructureTransformer, vocab_size: int):
        super().__init__()
        self.encoder = encoder
        self.mlm_head = nn.Linear(encoder.feature_dim, vocab_size)

    def forward(
        self,
        token_ids: torch.Tensor,
        depth_ids: torch.Tensor,
        type_ids: torch.Tensor,
        attention_mask: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        cls_feature, encoded_tokens = self.encoder(
            token_ids,
            depth_ids,
            type_ids,
            attention_mask,
        )
        logits = self.mlm_head(encoded_tokens)
        return logits, cls_feature


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-depth", type=int, default=128)
    parser.add_argument("--feature-dim", type=int, default=128)
    parser.add_argument("--num-heads", type=int, default=8)
    parser.add_argument("--num-layers", type=int, default=4)
    parser.add_argument("--feedforward-dim", type=int, default=256)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--hash-buckets", type=int, default=65536)
    parser.add_argument("--max-component-tokens", type=int, default=16)
    parser.add_argument("--mask-prob", type=float, default=0.15)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--device", default=None)
    parser.add_argument("--resume")
    return parser.parse_args()


def resolve_device(device: str | None) -> torch.device:
    return torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))


def load_resume_checkpoint(path: str | Path | None, device: torch.device) -> dict[str, Any] | None:
    if not path:
        return None
    return torch.load(Path(path), map_location=device)


def build_tokenizer(args: argparse.Namespace, checkpoint: dict[str, Any] | None) -> PathStructureTokenizer:
    tokenizer_info = (checkpoint or {}).get("tokenizer_info", {})
    return PathStructureTokenizer(
        hash_bucket_count=int(tokenizer_info.get("hash_bucket_count", args.hash_buckets)),
        lowercase=bool(tokenizer_info.get("lowercase", True)),
        max_component_tokens=int(
            tokenizer_info.get("max_component_tokens", args.max_component_tokens)
        ),
    )


def build_model_config(args: argparse.Namespace, checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    source = checkpoint or {}
    return {
        "feature_dim": int(source.get("feature_dim", args.feature_dim)),
        "num_heads": int(source.get("num_heads", args.num_heads)),
        "num_layers": int(source.get("num_layers", args.num_layers)),
        "feedforward_dim": int(source.get("feedforward_dim", args.feedforward_dim)),
        "dropout": float(source.get("dropout", args.dropout)),
        "max_tokens": int(source.get("max_tokens", args.max_tokens)),
        "max_depth": int(source.get("max_depth", args.max_depth)),
        "use_projection_head": bool(source.get("use_projection_head", False)),
    }


def build_model(
    tokenizer: PathStructureTokenizer,
    config: dict[str, Any],
    device: torch.device,
) -> MaskedFolderModel:
    if config["feature_dim"] % config["num_heads"] != 0:
        raise ValueError("feature_dim must be divisible by num_heads.")

    encoder = FileStructureTransformer(
        vocab_size=tokenizer.vocab_size + 1,
        feature_dim=config["feature_dim"],
        num_heads=config["num_heads"],
        num_layers=config["num_layers"],
        feedforward_dim=config["feedforward_dim"],
        dropout=config["dropout"],
        max_tokens=config["max_tokens"],
        max_depth=config["max_depth"],
        use_projection_head=config["use_projection_head"],
    )
    return MaskedFolderModel(encoder, tokenizer.vocab_size + 1).to(device)


def expand_token_embedding_if_needed(
    state_dict: dict[str, torch.Tensor],
    current_state_dict: dict[str, torch.Tensor],
) -> dict[str, torch.Tensor]:
    key = "token_embedding.weight"
    if key not in state_dict:
        return state_dict

    loaded_weight = state_dict[key]
    current_weight = current_state_dict[key]
    if loaded_weight.shape == current_weight.shape:
        return state_dict

    if loaded_weight.shape[1:] != current_weight.shape[1:]:
        return state_dict

    expanded = current_weight.clone()
    rows = min(loaded_weight.shape[0], expanded.shape[0])
    expanded[:rows] = loaded_weight[:rows]
    state_dict = dict(state_dict)
    state_dict[key] = expanded
    return state_dict


def load_training_state(
    model: MaskedFolderModel,
    optimizer: torch.optim.Optimizer,
    checkpoint: dict[str, Any] | None,
) -> int:
    if checkpoint is None:
        return 1

    encoder_state = checkpoint.get("training_model_state_dict") or checkpoint["model_state_dict"]
    encoder_state = expand_token_embedding_if_needed(
        encoder_state,
        model.encoder.state_dict(),
    )
    model.encoder.load_state_dict(encoder_state)

    if "mlm_head_state_dict" in checkpoint:
        model.mlm_head.load_state_dict(checkpoint["mlm_head_state_dict"])
    if "optimizer_state_dict" in checkpoint:
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])

    return int(checkpoint.get("epoch", 0)) + 1


def inference_encoder_state_dict(
    encoder: FileStructureTransformer,
    tokenizer: PathStructureTokenizer,
) -> dict[str, torch.Tensor]:
    state_dict = {
        key: value.detach().cpu().clone()
        for key, value in encoder.state_dict().items()
    }
    embedding_key = "token_embedding.weight"
    if state_dict[embedding_key].shape[0] > tokenizer.vocab_size:
        state_dict[embedding_key] = state_dict[embedding_key][: tokenizer.vocab_size].clone()
    return state_dict


def training_encoder_state_dict(encoder: FileStructureTransformer) -> dict[str, torch.Tensor]:
    return {
        key: value.detach().cpu().clone()
        for key, value in encoder.state_dict().items()
    }


def module_state_dict(module: nn.Module) -> dict[str, torch.Tensor]:
    return {
        key: value.detach().cpu().clone()
        for key, value in module.state_dict().items()
    }


def save_checkpoint(
    output_path: Path,
    model: MaskedFolderModel,
    optimizer: torch.optim.Optimizer,
    tokenizer: PathStructureTokenizer,
    config: dict[str, Any],
    epoch: int,
    loss: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": inference_encoder_state_dict(model.encoder, tokenizer),
            "training_model_state_dict": training_encoder_state_dict(model.encoder),
            "mlm_head_state_dict": module_state_dict(model.mlm_head),
            "optimizer_state_dict": optimizer.state_dict(),
            "epoch": epoch,
            "loss": loss,
            "feature_dim": config["feature_dim"],
            "num_heads": config["num_heads"],
            "num_layers": config["num_layers"],
            "feedforward_dim": config["feedforward_dim"],
            "dropout": config["dropout"],
            "max_tokens": config["max_tokens"],
            "max_depth": config["max_depth"],
            "use_projection_head": config["use_projection_head"],
            "tokenizer_info": tokenizer.tokenizer_info(),
            "mask_token_id": tokenizer.vocab_size,
            "training_vocab_size": tokenizer.vocab_size + 1,
        },
        output_path,
    )


def move_batch_to_device(
    batch: dict[str, torch.Tensor],
    device: torch.device,
) -> dict[str, torch.Tensor]:
    return {
        key: value.to(device, non_blocking=True)
        for key, value in batch.items()
    }


def train_epoch(
    model: MaskedFolderModel,
    dataloader: DataLoader,
    optimizer: torch.optim.Optimizer,
    criterion: nn.Module,
    device: torch.device,
) -> float:
    model.train()
    total_loss = 0.0
    step_count = 0

    for batch in dataloader:
        batch = move_batch_to_device(batch, device)
        if not (batch["labels"] != -100).any():
            continue

        optimizer.zero_grad(set_to_none=True)
        logits, _cls_feature = model(
            batch["input_token_ids"],
            batch["depth_ids"],
            batch["type_ids"],
            batch["attention_mask"],
        )
        loss = criterion(
            logits.reshape(-1, logits.shape[-1]),
            batch["labels"].reshape(-1),
        )
        loss.backward()
        optimizer.step()

        total_loss += float(loss.detach().cpu())
        step_count += 1

    if step_count == 0:
        raise RuntimeError("No masked tokens were generated for training.")

    return total_loss / step_count


def main() -> None:
    args = parse_args()
    device = resolve_device(args.device)
    resume_checkpoint = load_resume_checkpoint(args.resume, device)
    tokenizer = build_tokenizer(args, resume_checkpoint)
    config = build_model_config(args, resume_checkpoint)
    model = build_model(tokenizer, config, device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=args.lr,
        weight_decay=args.weight_decay,
    )
    start_epoch = load_training_state(model, optimizer, resume_checkpoint)
    dataset = FolderStructureDataset(args.data_root, tokenizer)
    collator = MaskedFolderCollator(
        tokenizer=tokenizer,
        max_tokens=config["max_tokens"],
        max_depth=config["max_depth"],
        mask_prob=args.mask_prob,
        mask_token_id=tokenizer.vocab_size,
    )
    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        collate_fn=collator,
        pin_memory=device.type == "cuda",
    )
    criterion = nn.CrossEntropyLoss(ignore_index=-100)
    output_dir = Path(args.output_dir).expanduser().resolve(strict=False)
    last_loss = float((resume_checkpoint or {}).get("loss", 0.0))
    last_epoch = start_epoch - 1

    for epoch in range(start_epoch, args.epochs + 1):
        last_loss = train_epoch(model, dataloader, optimizer, criterion, device)
        last_epoch = epoch
        print(f"epoch {epoch}/{args.epochs} loss={last_loss:.6f}")

        epoch_checkpoint = output_dir / f"checkpoint_epoch_{epoch}.pt"
        latest_checkpoint = output_dir / "latest.pt"
        save_checkpoint(epoch_checkpoint, model, optimizer, tokenizer, config, epoch, last_loss)
        shutil.copyfile(epoch_checkpoint, latest_checkpoint)

    save_checkpoint(output_dir / "final.pt", model, optimizer, tokenizer, config, last_epoch, last_loss)


if __name__ == "__main__":
    main()

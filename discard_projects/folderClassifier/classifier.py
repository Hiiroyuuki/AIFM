from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
from sklearn.metrics import classification_report, f1_score, hamming_loss
from sklearn.model_selection import train_test_split
from sklearn.multiclass import OneVsRestClassifier
from xgboost import XGBClassifier

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from folderClassifier.encoder.transformer import FolderTransformerFeatureExtractor
except ImportError:
    from encoder.transformer import FolderTransformerFeatureExtractor


LABELS = (
    "app",
    "db",
    "cache",
    "user_data",
    "media",
    "document",
)


@dataclass(frozen=True)
class LabeledFolderSample:
    path: Path
    labels: tuple[str, ...]


@dataclass(frozen=True)
class CachedFeatureSample:
    path: str
    labels: tuple[str, ...]
    feature: list[float]


class FolderLabelDataset:
    def __init__(
        self,
        data_root: str | Path | None = None,
        manifest_path: str | Path | None = None,
    ):
        self.data_root = Path(data_root).expanduser().resolve(strict=False) if data_root else None
        self.manifest_path = (
            Path(manifest_path).expanduser().resolve(strict=False)
            if manifest_path
            else None
        )
        self.samples = self.load_samples()
        if not self.samples:
            raise ValueError("No labeled folder samples were found.")

    def load_samples(self) -> list[LabeledFolderSample]:
        if self.manifest_path:
            return self.load_manifest(self.manifest_path)
        if self.data_root:
            return self.load_label_folders(self.data_root)
        raise ValueError("Provide either data_root or manifest_path.")

    def load_label_folders(self, data_root: Path) -> list[LabeledFolderSample]:
        if not data_root.is_dir():
            raise NotADirectoryError(str(data_root))

        samples = []
        for label in LABELS:
            label_root = data_root / label
            if not label_root.is_dir():
                continue

            child_folders = sorted(
                (path.resolve(strict=False) for path in label_root.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            )
            if child_folders:
                samples.extend(
                    LabeledFolderSample(path, (label,))
                    for path in child_folders
                )
            else:
                samples.append(LabeledFolderSample(label_root.resolve(strict=False), (label,)))

        return samples

    def load_manifest(self, manifest_path: Path) -> list[LabeledFolderSample]:
        if not manifest_path.is_file():
            raise FileNotFoundError(str(manifest_path))

        suffix = manifest_path.suffix.lower()
        if suffix == ".json":
            return self.load_json_manifest(manifest_path)
        if suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            return self.load_csv_manifest(manifest_path, delimiter)

        raise ValueError("Manifest must be .json, .csv, or .tsv.")

    def load_json_manifest(self, manifest_path: Path) -> list[LabeledFolderSample]:
        with manifest_path.open("r", encoding="utf-8") as file:
            data = json.load(file)

        samples = []
        if isinstance(data, dict):
            for labels, paths in data.items():
                for path in paths:
                    samples.append(self.make_sample(path, labels))
            return samples

        if isinstance(data, list):
            for item in data:
                labels = item.get("labels", item.get("label"))
                samples.append(self.make_sample(item["path"], labels))
            return samples

        raise ValueError("JSON manifest must be a list or a label-to-paths dict.")

    def load_csv_manifest(
        self,
        manifest_path: Path,
        delimiter: str,
    ) -> list[LabeledFolderSample]:
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, delimiter=delimiter)
            if reader.fieldnames and "path" in reader.fieldnames:
                label_field = "labels" if "labels" in reader.fieldnames else "label"
                if label_field not in reader.fieldnames:
                    raise ValueError("CSV manifest must contain a labels column.")
                return [
                    self.make_sample(row["path"], row[label_field])
                    for row in reader
                    if row.get("path") and row.get(label_field)
                ]

        with manifest_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file, delimiter=delimiter)
            return [
                self.make_sample(row[0], row[1])
                for row in reader
                if len(row) >= 2 and row[0] != "path"
            ]

    def make_sample(self, path: str | Path, labels: Any) -> LabeledFolderSample:
        normalized_labels = self.parse_labels(labels)

        folder_path = Path(path).expanduser().resolve(strict=False)
        if not folder_path.is_dir():
            raise NotADirectoryError(str(folder_path))

        return LabeledFolderSample(folder_path, normalized_labels)

    @staticmethod
    def parse_labels(labels: Any) -> tuple[str, ...]:
        if isinstance(labels, str):
            raw_labels = labels.split(",")
        elif isinstance(labels, (list, tuple, set)):
            raw_labels = list(labels)
        else:
            raise ValueError("labels must be a list or a comma-separated string.")

        normalized_labels = []
        for label in raw_labels:
            normalized_label = str(label).strip().lower()
            if not normalized_label:
                continue
            if normalized_label not in LABELS:
                raise ValueError(
                    f"Unknown label '{label}'. Available labels: {', '.join(LABELS)}"
                )
            normalized_labels.append(normalized_label)

        if not normalized_labels:
            raise ValueError("labels cannot be empty.")

        return tuple(dict.fromkeys(normalized_labels))


class FolderFeatureCsvDataset:
    def __init__(self, csv_path: str | Path):
        self.csv_path = Path(csv_path).expanduser().resolve(strict=False)
        self.samples = self.load_samples()
        if not self.samples:
            raise ValueError("No cached feature samples were found.")

    def load_samples(self) -> list[CachedFeatureSample]:
        if not self.csv_path.is_file():
            raise FileNotFoundError(str(self.csv_path))

        samples = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            required_fields = {"path", "labels", "feature_json"}
            if not reader.fieldnames or not required_fields.issubset(set(reader.fieldnames)):
                raise ValueError("Feature CSV must contain path, labels, and feature_json columns.")

            for row in reader:
                labels = FolderLabelDataset.parse_labels(row["labels"])
                feature = json.loads(row["feature_json"])
                if not isinstance(feature, list) or not feature:
                    raise ValueError(f"feature_json must be a non-empty list: {row.get('path')}")

                samples.append(
                    CachedFeatureSample(
                        path=row["path"],
                        labels=labels,
                        feature=[float(value) for value in feature],
                    )
                )

        return samples

    def get_features_and_labels(
        self,
        classifier: TransformerXGBoostFolderClassifier,
    ) -> tuple[list[list[float]], list[list[int]]]:
        features = [sample.feature for sample in self.samples]
        labels = [classifier.encode_labels(sample.labels) for sample in self.samples]
        return features, labels


class TransformerXGBoostFolderClassifier:
    def __init__(
        self,
        encoder_checkpoint: str | Path,
        device: str | None = None,
        xgb_params: dict[str, Any] | None = None,
    ):
        self.encoder_checkpoint = str(Path(encoder_checkpoint).expanduser().resolve(strict=False))
        self.device = device
        self.extractor = FolderTransformerFeatureExtractor(
            checkpoint_path=self.encoder_checkpoint,
            device=device,
        )
        self.label_to_id = {label: index for index, label in enumerate(LABELS)}
        self.id_to_label = {index: label for label, index in self.label_to_id.items()}
        self.xgb_params = xgb_params or {}
        self.model = None

    def extract_feature(self, folder_path: str | Path) -> list[float]:
        result = self.extractor.encode_folder(folder_path)
        return result["feature"]

    def encode_labels(self, labels: tuple[str, ...]) -> list[int]:
        vector = [0] * len(LABELS)
        for label in labels:
            vector[self.label_to_id[label]] = 1
        return vector

    def extract_features(
        self,
        samples: list[LabeledFolderSample],
        verbose: bool = True,
        progress_interval: int = 10,
    ) -> tuple[list[list[float]], list[list[int]]]:
        features = []
        labels = []
        total = len(samples)
        for index, sample in enumerate(samples, start=1):
            should_print = (
                progress_interval > 0
                and (index == 1 or index == total or index % progress_interval == 0)
            )
            if verbose and should_print:
                print(
                    f"[feature] {index}/{total} labels={','.join(sample.labels)} path={sample.path}",
                    flush=True,
                )
            features.append(self.extract_feature(sample.path))
            labels.append(self.encode_labels(sample.labels))
        if verbose:
            print(f"[feature] completed {total} folders", flush=True)
        return features, labels

    def evaluate(
        self,
        split_name: str,
        features: list[list[float]],
        labels: list[list[int]],
    ) -> dict[str, Any]:
        if self.model is None:
            raise RuntimeError("Classifier model has not been trained.")

        predicted = self.model.predict(features)
        micro_f1 = f1_score(labels, predicted, average="micro", zero_division=0)
        macro_f1 = f1_score(labels, predicted, average="macro", zero_division=0)
        loss = hamming_loss(labels, predicted)
        text_report = classification_report(
            labels,
            predicted,
            target_names=list(LABELS),
            zero_division=0,
        )
        dict_report = classification_report(
            labels,
            predicted,
            target_names=list(LABELS),
            zero_division=0,
            output_dict=True,
        )
        print(
            f"[metrics] {split_name} micro_f1={micro_f1:.4f} "
            f"macro_f1={macro_f1:.4f} hamming_loss={loss:.4f}",
            flush=True,
        )
        print(text_report, flush=True)
        return {
            "micro_f1": micro_f1,
            "macro_f1": macro_f1,
            "hamming_loss": loss,
            "classification_report": dict_report,
        }

    def fit(
        self,
        samples: list[LabeledFolderSample],
        test_size: float = 0.2,
        random_state: int = 42,
        verbose: bool = True,
        progress_interval: int = 10,
    ) -> dict[str, Any]:
        if verbose:
            label_counts = Counter(label for sample in samples for label in sample.labels)
            print(f"[data] samples={len(samples)} labels={dict(label_counts)}", flush=True)
            print(f"[encoder] checkpoint={self.encoder_checkpoint}", flush=True)
        features, labels = self.extract_features(
            samples,
            verbose=verbose,
            progress_interval=progress_interval,
        )
        return self.fit_features(
            features,
            labels,
            test_size=test_size,
            random_state=random_state,
            verbose=verbose,
        )

    def fit_features(
        self,
        features: list[list[float]],
        labels: list[list[int]],
        test_size: float = 0.2,
        random_state: int = 42,
        verbose: bool = True,
    ) -> dict[str, Any]:
        if not features:
            raise ValueError("features cannot be empty.")
        if len(features) != len(labels):
            raise ValueError("features and labels must have the same length.")

        self.model = OneVsRestClassifier(
            XGBClassifier(
                objective="binary:logistic",
                eval_metric="logloss",
                **self.xgb_params,
            )
        )

        train_report = {}
        test_report = {}
        if test_size > 0 and len(features) >= 2:
            x_train, x_test, y_train, y_test = train_test_split(
                features,
                labels,
                test_size=test_size,
                random_state=random_state,
                stratify=None,
            )
            if verbose:
                print(
                    f"[train] fitting XGBoost train={len(x_train)} test={len(x_test)}",
                    flush=True,
                )
            self.model.fit(x_train, y_train)
            if verbose:
                print("[train] XGBoost fit completed", flush=True)
            train_report = self.evaluate("train", x_train, y_train)
            test_report = self.evaluate("test", x_test, y_test)
        else:
            if verbose:
                print(f"[train] fitting XGBoost train={len(features)} test=0", flush=True)
            self.model.fit(features, labels)
            if verbose:
                print("[train] XGBoost fit completed", flush=True)
            train_report = self.evaluate("train", features, labels)

        return {
            "sample_count": len(features),
            "labels": list(LABELS),
            "train_report": train_report,
            "test_report": test_report,
        }

    def predict(self, folder_path: str | Path, threshold: float = 0.5) -> dict[str, Any]:
        if self.model is None:
            raise RuntimeError("Classifier model has not been loaded or trained.")

        feature = [self.extract_feature(folder_path)]
        probabilities = self.model.predict_proba(feature)[0]
        scores = {
            self.id_to_label[index]: float(probability)
            for index, probability in enumerate(probabilities)
        }
        selected_labels = [
            label
            for label, probability in scores.items()
            if probability >= threshold
        ]
        if not selected_labels:
            selected_labels = [max(scores, key=scores.get)]

        return {
            "path": str(Path(folder_path).expanduser().resolve(strict=False)),
            "labels": selected_labels,
            "scores": scores,
            "threshold": threshold,
        }

    def save(self, output_path: str | Path) -> None:
        if self.model is None:
            raise RuntimeError("Classifier model has not been trained.")

        output = Path(output_path).expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "encoder_checkpoint": self.encoder_checkpoint,
                "device": self.device,
                "labels": list(LABELS),
                "xgb_params": self.xgb_params,
            },
            output,
        )

    @classmethod
    def load(
        cls,
        model_path: str | Path,
        encoder_checkpoint: str | Path | None = None,
        device: str | None = None,
    ) -> TransformerXGBoostFolderClassifier:
        payload = joblib.load(Path(model_path).expanduser().resolve(strict=False))
        classifier = cls(
            encoder_checkpoint=encoder_checkpoint or payload["encoder_checkpoint"],
            device=device if device is not None else payload.get("device"),
            xgb_params=payload.get("xgb_params", {}),
        )
        classifier.model = payload["model"]
        return classifier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    train_parser = subparsers.add_parser("train")
    train_parser.add_argument("--data-root")
    train_parser.add_argument("--manifest")
    train_parser.add_argument("--feature-csv")
    train_parser.add_argument("--encoder-checkpoint", required=True)
    train_parser.add_argument("--output-model", required=True)
    train_parser.add_argument("--device", default=None)
    train_parser.add_argument("--test-size", type=float, default=0.2)
    train_parser.add_argument("--random-state", type=int, default=42)
    train_parser.add_argument("--n-estimators", type=int, default=300)
    train_parser.add_argument("--max-depth", type=int, default=4)
    train_parser.add_argument("--learning-rate", type=float, default=0.05)
    train_parser.add_argument("--subsample", type=float, default=0.9)
    train_parser.add_argument("--colsample-bytree", type=float, default=0.9)
    train_parser.add_argument("--tree-method", default="hist")
    train_parser.add_argument("--progress-interval", type=int, default=10)
    train_parser.add_argument("--quiet", action="store_true")

    predict_parser = subparsers.add_parser("predict")
    predict_parser.add_argument("--model", required=True)
    predict_parser.add_argument("--folder", required=True)
    predict_parser.add_argument("--encoder-checkpoint")
    predict_parser.add_argument("--device", default=None)
    predict_parser.add_argument("--threshold", type=float, default=0.5)

    build_parser = subparsers.add_parser("build-feature-csv")
    build_parser.add_argument("--manifest", required=True)
    build_parser.add_argument("--encoder-checkpoint", required=True)
    build_parser.add_argument("--output-csv", required=True)
    build_parser.add_argument("--device", default=None)
    build_parser.add_argument("--progress-interval", type=int, default=10)
    build_parser.add_argument("--quiet", action="store_true")

    return parser.parse_args()


def train_command(args: argparse.Namespace) -> None:
    classifier = TransformerXGBoostFolderClassifier(
        encoder_checkpoint=args.encoder_checkpoint,
        device=args.device,
        xgb_params={
            "n_estimators": args.n_estimators,
            "max_depth": args.max_depth,
            "learning_rate": args.learning_rate,
            "subsample": args.subsample,
            "colsample_bytree": args.colsample_bytree,
            "tree_method": args.tree_method,
        },
    )
    if args.feature_csv:
        dataset = FolderFeatureCsvDataset(args.feature_csv)
        features, labels = dataset.get_features_and_labels(classifier)
        if not args.quiet:
            label_counts = Counter(label for sample in dataset.samples for label in sample.labels)
            print(f"[dataset] loaded {len(dataset.samples)} cached feature rows", flush=True)
            print(f"[dataset] label distribution {dict(label_counts)}", flush=True)
            print(f"[feature-csv] source={args.feature_csv}", flush=True)
        result = classifier.fit_features(
            features,
            labels,
            test_size=args.test_size,
            random_state=args.random_state,
            verbose=not args.quiet,
        )
    else:
        dataset = FolderLabelDataset(data_root=args.data_root, manifest_path=args.manifest)
        if not args.quiet:
            label_counts = Counter(label for sample in dataset.samples for label in sample.labels)
            print(f"[dataset] loaded {len(dataset.samples)} samples", flush=True)
            print(f"[dataset] label distribution {dict(label_counts)}", flush=True)
        result = classifier.fit(
            dataset.samples,
            test_size=args.test_size,
            random_state=args.random_state,
            verbose=not args.quiet,
            progress_interval=args.progress_interval,
        )
    if not args.quiet:
        print(f"[save] output_model={args.output_model}", flush=True)
    classifier.save(args.output_model)
    print(json.dumps(result, ensure_ascii=False, indent=4))


def predict_command(args: argparse.Namespace) -> None:
    classifier = TransformerXGBoostFolderClassifier.load(
        model_path=args.model,
        encoder_checkpoint=args.encoder_checkpoint,
        device=args.device,
    )
    result = classifier.predict(args.folder, threshold=args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=4))


def build_feature_csv_command(args: argparse.Namespace) -> None:
    dataset = FolderLabelDataset(manifest_path=args.manifest)
    classifier = TransformerXGBoostFolderClassifier(
        encoder_checkpoint=args.encoder_checkpoint,
        device=args.device,
    )
    output_csv = Path(args.output_csv).expanduser().resolve(strict=False)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    success = 0
    errors = []
    total = len(dataset.samples)

    with output_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["path", "labels", "feature_json"])
        writer.writeheader()
        for index, sample in enumerate(dataset.samples, start=1):
            should_print = (
                args.progress_interval > 0
                and (index == 1 or index == total or index % args.progress_interval == 0)
            )
            if not args.quiet and should_print:
                print(
                    f"[feature-csv] {index}/{total} labels={','.join(sample.labels)} path={sample.path}",
                    flush=True,
                )

            try:
                result = classifier.extractor.encode_folder(sample.path)
                feature = result["feature"]
                writer.writerow(
                    {
                        "path": str(sample.path),
                        "labels": ",".join(sample.labels),
                        "feature_json": json.dumps(feature, ensure_ascii=False),
                    }
                )
                success += 1
            except Exception as error:
                errors.append(
                    {
                        "path": str(sample.path),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                if not args.quiet:
                    print(
                        f"[feature-csv] failed path={sample.path} error={type(error).__name__}: {error}",
                        flush=True,
                    )

    summary = {
        "total": total,
        "success": success,
        "failed": len(errors),
        "output_csv": str(output_csv),
    }
    if errors:
        summary["errors"] = errors

    print(json.dumps(summary, ensure_ascii=False, indent=4))


def main() -> None:
    args = parse_args()
    if args.command == "train":
        train_command(args)
    elif args.command == "predict":
        predict_command(args)
    elif args.command == "build-feature-csv":
        build_feature_csv_command(args)


if __name__ == "__main__":
    main()

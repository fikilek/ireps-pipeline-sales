import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--clean-output", required=True)
parser.add_argument("--upload-output", required=True)
parser.add_argument("--expected-count", required=True, type=int)
args = parser.parse_args()

source = Path(args.input)
clean_output = Path(args.clean_output)
upload_output = Path(args.upload_output)

clean_output.parent.mkdir(parents=True, exist_ok=True)
upload_output.parent.mkdir(parents=True, exist_ok=True)

removed = 0
count = 0
seen = set()

def clean(value):
    global removed

    if isinstance(value, dict):
        result = {}

        for key, child in value.items():
            if key.lower() in {"geometry", "geometryjson"}:
                removed += 1
                continue

            result[key] = clean(child)

        return result

    if isinstance(value, list):
        return [clean(item) for item in value]

    return value

def validate(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"geometry", "geometryjson"}:
                raise RuntimeError(
                    f"Geometry remains at {path}.{key}"
                )

            validate(child, f"{path}.{key}")

    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, list):
                raise RuntimeError(
                    f"Nested array remains at {path}[{index}]"
                )

            validate(child, f"{path}[{index}]")

with (
    source.open("r", encoding="utf-8") as reader,
    clean_output.open("w", encoding="utf-8", newline="\n") as clean_writer,
    upload_output.open("w", encoding="utf-8", newline="\n") as upload_writer,
):

    for line_number, line in enumerate(reader, start=1):
        if not line.strip():
            continue

        data = clean(json.loads(line))
        validate(data)

        doc_id = str(data.get("MeterNumber", "")).strip()

        if not doc_id:
            raise RuntimeError(
                f"Missing MeterNumber at line {line_number}"
            )

        if doc_id in seen:
            raise RuntimeError(
                f"Duplicate MeterNumber: {doc_id}"
            )

        seen.add(doc_id)

        clean_writer.write(
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        )

        upload_writer.write(
            json.dumps(
                {
                    "docId": doc_id,
                    "data": data,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        )

        count += 1

        if count % 2000 == 0:
            print(
                f"[PROGRESS] Prepared {count:,} documents"
            )

if count != args.expected_count:
    raise RuntimeError(
        f"Expected {args.expected_count:,} documents "
        f"but created {count:,}"
    )

print("")
print("GEOMETRY-FREE PSD COMPLETED")
print(f"Documents preserved:     {count:,}")
print(f"Unique document IDs:     {len(seen):,}")
print(f"Geometry fields removed: {removed:,}")
print("Geometry validation:     PASSED")
print("Nested-array validation: PASSED")
print(
    "Clean PSD SHA-256:      "
    + hashlib.sha256(clean_output.read_bytes()).hexdigest()
)
print(
    "Upload SHA-256:         "
    + hashlib.sha256(upload_output.read_bytes()).hexdigest()
)
print("Firestore operations:    NONE")


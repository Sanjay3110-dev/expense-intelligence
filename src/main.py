"""
Single-file DWP Government Procurement Card (GPC) data pipeline.

Commands:
    python main.py load --raw-dir data/raw --output data/processed/gpc_clean.csv
    python main.py categorize --backend rules
    python main.py categorize --backend llm
    python main.py build-db
    python main.py pipeline --backend rules

For the LLM backend, set:
    LLM_BASE_URL=https://api.groq.com/openai/v1
    LLM_API_KEY=your_api_key
    LLM_MODEL=llama-3.3-70b-versatile

Install dependencies:
    pip install pandas duckdb
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import urllib.request
from pathlib import Path

import pandas as pd


# =============================================================================
# 1. CONFIGURATION
# =============================================================================

RAW_DIR = "data/raw"
CLEAN_CSV = "data/processed/gpc_clean.csv"
LLM_CACHE = "data/processed/llm_cache.json"
DB_PATH = "data/gpc.duckdb"

BATCH_SIZE = 25
BOILERPLATE = "Goods and services that help move customers closer to the labour market"


# =============================================================================
# 2. TAXONOMY: MCC -> CATEGORY MAPPING
# =============================================================================

MCC_TO_CATEGORY = {
    # Visa & immigration fees
    9399: "Visa & Immigration Fees",

    # Rail & road travel
    4112: "Rail & Road Travel",
    4784: "Rail & Road Travel",
    7523: "Rail & Road Travel",

    # Accommodation & booking platforms
    4722: "Accommodation & Booking",

    # Tools & trade equipment
    5211: "Tools & Trade Equipment",
    5251: "Tools & Trade Equipment",
    5072: "Tools & Trade Equipment",
    5085: "Tools & Trade Equipment",
    5046: "Tools & Trade Equipment",
    5065: "Tools & Trade Equipment",
    7394: "Tools & Trade Equipment",
    5039: "Tools & Trade Equipment",

    # Electronics & IT
    5732: "Electronics & IT",
    5045: "Electronics & IT",
    5734: "Electronics & IT",
    5946: "Electronics & IT",
    4812: "Electronics & IT",

    # General retail & household
    5311: "General Retail & Household",
    5399: "General Retail & Household",
    5712: "General Retail & Household",
    5169: "General Retail & Household",
    5999: "General Retail & Household",
    5977: "General Retail & Household",
    5970: "General Retail & Household",
    5021: "General Retail & Household",
    5969: "General Retail & Household",

    # Vehicles & cycles
    5533: "Vehicles & Cycles",
    5571: "Vehicles & Cycles",

    # Publications, training & professional fees
    5815: "Publications & Training",
    8299: "Publications & Training",
    8398: "Publications & Training",
}

OTHER = "Other"
CATEGORIES = sorted(set(MCC_TO_CATEGORY.values())) + [OTHER]


def truth(mcc: int) -> str:
    """Return the MCC-derived category used as the evaluation label."""
    return MCC_TO_CATEGORY.get(int(mcc), OTHER)


# =============================================================================
# 3. CSV LOADING, NORMALISATION AND DEDUPLICATION
# =============================================================================

COLMAP = {
    "line number": "line_no",
    "posting date": "posting_date",
    "transaction ref number": "txn_ref",
    "transaction reference number": "txn_ref",
    "mch.merchant category code (mcc)": "mcc",
    "mch.merchant name": "merchant",
    "fin.transaction amount": "amount",
    "description": "description",
}


def read_one(path: str) -> pd.DataFrame:
    """Read one source CSV, supporting UTF-8 and Windows-1252."""
    df = None

    for encoding in ("utf-8-sig", "cp1252"):
        try:
            df = pd.read_csv(
                path,
                encoding=encoding,
                dtype=str,
                keep_default_na=False,
            )
            break
        except UnicodeDecodeError:
            continue

    if df is None:
        raise ValueError(f"Could not decode CSV file: {path}")

    normalised_columns = []
    for column in df.columns:
        key = column.strip().lstrip("\ufeff").lower()
        if key not in COLMAP:
            raise ValueError(f"Unexpected column {column!r} in {path}")
        normalised_columns.append(COLMAP[key])

    df.columns = normalised_columns
    df = df[df["amount"].str.strip() != ""].copy()
    df["source_file"] = os.path.basename(path)
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise transaction fields and data types."""
    out = df.copy()

    out["line_no"] = out["line_no"].str.strip()
    out["txn_ref"] = (
        out["txn_ref"]
        .str.strip()
        .str.replace(r"\.0+$", "", regex=True)
    )
    out["mcc"] = out["mcc"].astype(int)
    out["merchant"] = (
        out["merchant"].str.strip().str.replace(r"\s+", " ", regex=True)
    )
    out["description"] = (
        out["description"].str.strip().str.replace(r"\s+", " ", regex=True)
    )
    out["amount"] = (
        out["amount"]
        .str.replace("£", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip()
        .astype(float)
    )
    out["posting_date"] = pd.to_datetime(
        out["posting_date"].str.strip(),
        format="%d/%m/%Y",
    )

    return out


def load_transactions(raw_dir: str, output_path: str) -> pd.DataFrame:
    """Load all monthly CSVs, clean them, remove overlap duplicates and save."""
    files = sorted(glob.glob(os.path.join(raw_dir, "*.csv")))

    if not files:
        raise FileNotFoundError(f"No .csv files found in directory: {raw_dir}")

    frames = [clean(read_one(path)) for path in files]
    raw = pd.concat(frames, ignore_index=True)

    dedupe_key = ["txn_ref", "posting_date", "amount", "merchant"]
    duplicate_mask = raw.duplicated(subset=dedupe_key, keep="first")

    deduped = (
        raw[~duplicate_mask]
        .sort_values(
            ["posting_date", "amount"],
            ascending=[True, False],
        )
        .reset_index(drop=True)
    )
    deduped.insert(0, "txn_id", range(1, len(deduped) + 1))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    deduped.to_csv(output, index=False)

    print(f"Files read:             {len(files)}")
    print(f"Raw rows:               {len(raw)}")
    print(f"Overlap duplicates:     {int(duplicate_mask.sum())}")
    print(f"Clean rows:             {len(deduped)}")

    if not deduped.empty:
        print(
            "Date range:             "
            f"{deduped.posting_date.min().date()} -> "
            f"{deduped.posting_date.max().date()}"
        )
        print(f"Total spend (GBP):      {deduped.amount.sum():,.2f}")

    print("\nRows per source file:")
    print(raw.groupby("source_file").size().to_string())

    print("\nOverlap duplicates by source file:")
    duplicate_counts = raw[duplicate_mask].groupby("source_file").size()
    print(duplicate_counts.to_string() if not duplicate_counts.empty else "none")

    print("\nSpend by MCC (top 12):")
    top = (
        deduped.groupby("mcc")
        .agg(rows=("amount", "size"), spend=("amount", "sum"))
        .sort_values("spend", ascending=False)
        .head(12)
    )
    print(top.round(2).to_string())

    print("\nRows sharing a txn_ref with a different merchant/amount:")
    same_ref = (
        deduped[deduped.duplicated("txn_ref", keep=False)]
        .sort_values("txn_ref")
    )
    print(len(same_ref))
    if not same_ref.empty:
        print(
            same_ref[
                ["txn_ref", "posting_date", "merchant", "amount", "mcc"]
            ].to_string(index=False)
        )

    masked = deduped[
        deduped.merchant.str.contains(
            "NOT ELSEWHERE CLASSIFIED|DIGITAL GOODS",
            regex=True,
        )
    ]
    print(
        "\nRows where merchant name is a generic MCC label "
        f"instead of a real name: {len(masked)}"
    )

    boilerplate_rows = deduped.description.str.startswith(
        "Goods and services that help move customers"
    )
    print(
        "Rows with the identical Flexible Support Fund boilerplate "
        f"description: {int(boilerplate_rows.sum())} of {len(deduped)}"
    )

    print(f"\nCleaned data saved to: {output_path}")
    return deduped


# =============================================================================
# 4. RULE-BASED CATEGORISATION
# =============================================================================

# Deliberately modest keyword rules: the MCC mapping is not used for prediction.
RULES = [
    ("Visa & Immigration Fees", ["UKVI", "VISA SPONSOR", "GOVERNMENT SERVICES"]),
    (
        "Rail & Road Travel",
        [
            "TRAINLINE", "RAIL", "GREATER ANGLIA", "C2C", "THAMESLINK",
            "GWR", "TUNNEL", "PARKING", "PERMIT",
        ],
    ),
    ("Accommodation & Booking", ["BOOKING", "AIRBNB", "HOTEL", "TRIP.COM"]),
    ("Tools & Trade Equipment", ["SCREWFIX", "TOOL", "B&Q", "WELDING"]),
    (
        "Electronics & IT",
        ["CURRYS", "APPLE", "DELL", "ASUS", "BACK MARKET", "CAMERA"],
    ),
    ("Vehicles & Cycles", ["HALFORDS", "MOTOR", "BIKES"]),
    (
        "Publications & Training",
        ["STATIONERY OFFICE", "PUBLICATION", "TRAINING", "COURSE", "DELEGATE"],
    ),
    (
        "General Retail & Household",
        ["ARGOS", "AMAZON", "JOHN LEWIS", "CLEANING"],
    ),
]


def rules_predict(merchant: str, description: str) -> str:
    """Predict a category using merchant and description keyword rules."""
    text = f"{merchant} {description}".upper()

    for category, keywords in RULES:
        if any(keyword in text for keyword in keywords):
            return category

    return OTHER


# =============================================================================
# 5. LLM-BASED CATEGORISATION AND CACHING
# =============================================================================

def _short_desc(desc: str) -> str:
    if desc.startswith(BOILERPLATE):
        return "(generic benefit-spend text)"
    return desc[:100]


def _chat(messages: list) -> str:
    """Call an OpenAI-compatible chat-completions endpoint."""
    required = ["LLM_BASE_URL", "LLM_MODEL"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise EnvironmentError(
            "Missing environment variable(s): " + ", ".join(missing)
        )

    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    body = json.dumps(
        {
            "model": os.environ["LLM_MODEL"],
            "messages": messages,
            "temperature": 0,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'none')}",
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)

    return payload["choices"][0]["message"]["content"]


def _parse(response_text: str) -> dict:
    match = re.search(r"\{.*\}", response_text, re.S)
    return json.loads(match.group(0)) if match else {}


def llm_predict(
    df: pd.DataFrame,
    cache_path: str = LLM_CACHE,
) -> pd.Series:
    """Categorise unique merchant/description pairs and cache the answers."""
    cache_file = Path(cache_path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    if cache_file.exists():
        with cache_file.open("r", encoding="utf-8") as file:
            cache = json.load(file)
    else:
        cache = {}

    keys = [
        f"{merchant}||{_short_desc(description)}"
        for merchant, description in zip(df.merchant, df.description)
    ]
    todo = sorted({key for key in keys if key not in cache})

    system_prompt = (
        "You categorise UK government purchase-card transactions for an "
        "expense audit. Use ONLY these categories, spelled exactly:\n- "
        + "\n- ".join(CATEGORIES)
        + "\nMerchant names are messy card-statement strings "
        "(prefixes like 'SP ', 'WWW.', trailing codes). "
        f"If nothing fits, answer '{OTHER}'. Reply with a single JSON object "
        'mapping each item number to its category, e.g. '
        '{"1": "Rail & Road Travel", "2": "Electronics & IT"}. No other text.'
    )

    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start : start + BATCH_SIZE]
        lines = []

        for number, key in enumerate(chunk, 1):
            merchant, description = key.split("||", 1)
            lines.append(
                f"{number}. merchant: {merchant} | description: {description}"
            )

        response_text = _chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n".join(lines)},
            ]
        )
        answers = _parse(response_text)

        for number, key in enumerate(chunk, 1):
            category = answers.get(str(number), OTHER)
            cache[key] = category if category in CATEGORIES else OTHER

        with cache_file.open("w", encoding="utf-8") as file:
            json.dump(cache, file, indent=1, ensure_ascii=False)

        print(
            "Categorised "
            f"{min(start + BATCH_SIZE, len(todo))}/{len(todo)} "
            "unique merchant strings"
        )

    return pd.Series([cache[key] for key in keys], index=df.index)


# =============================================================================
# 6. PREDICTION AND EVALUATION
# =============================================================================

def predict(df: pd.DataFrame, backend: str) -> pd.Series:
    if backend == "rules":
        return pd.Series(
            [
                rules_predict(merchant, description)
                for merchant, description in zip(df.merchant, df.description)
            ],
            index=df.index,
        )

    if backend == "llm":
        return llm_predict(df)

    raise ValueError(f"Unsupported backend: {backend}")


def evaluate(df: pd.DataFrame, predictions: pd.Series) -> None:
    """Evaluate predictions against MCC-derived labels (not shown to model)."""
    actual = df.mcc.map(truth)
    correct = predictions == actual

    print(f"Rows: {len(df)}   accuracy vs MCC labels: {correct.mean():.1%}")

    total_spend = df.amount.sum()
    if total_spend:
        spend_accuracy = df.loc[correct, "amount"].sum() / total_spend
        print(f"Spend-weighted accuracy: {spend_accuracy:.1%}")
    else:
        print("Spend-weighted accuracy: n/a (total spend is zero)")

    by_merchant = (
        pd.DataFrame({"merchant": df.merchant, "ok": correct})
        .groupby("merchant")
        .ok.mean()
    )
    print(
        "Merchant-level accuracy "
        f"(each of {len(by_merchant)} merchants once): "
        f"{by_merchant.mean():.1%}"
    )

    print()
    per_category = (
        pd.DataFrame({"truth": actual, "ok": correct})
        .groupby("truth")
        .agg(rows=("ok", "size"), correct=("ok", "sum"))
        .assign(
            accuracy=lambda table: (
                table.correct / table.rows
            ).round(3)
        )
        .sort_values("rows", ascending=False)
    )
    print(per_category.to_string())

    misses = df[~correct].copy()
    misses["truth"] = actual[~correct]
    misses["predicted"] = predictions[~correct]

    print()
    print(
        f"Misclassified: {len(misses)} "
        f"(unique merchants: {misses.merchant.nunique()})"
    )
    columns = ["merchant", "mcc", "truth", "predicted"]
    print(
        misses[columns]
        .drop_duplicates("merchant")
        .head(25)
        .to_string(index=False)
    )


def categorize_file(
    data_path: str = CLEAN_CSV,
    backend: str = "rules",
) -> pd.DataFrame:
    data = pd.read_csv(data_path)
    predictions = predict(data, backend)
    evaluate(data, predictions)

    result = data.copy()
    result["predicted_category"] = predictions
    return result


# =============================================================================
# 7. DUCKDB DATABASE BUILDING
# =============================================================================

def build_database(
    csv_path: str = CLEAN_CSV,
    db_path: str = DB_PATH,
) -> None:
    """Create the transactions and mcc_labels tables in DuckDB."""
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(
            "DuckDB is required for this command. Install it with: pip install duckdb"
        ) from exc

    if not Path(csv_path).exists():
        raise FileNotFoundError(
            f"Cleaned CSV not found: {csv_path}. Run the load command first."
        )

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_file))
    try:
        # Parameter binding avoids problems with quotes in file paths.
        con.execute("DROP TABLE IF EXISTS transactions")
        con.execute(
            "CREATE TABLE transactions AS "
            "SELECT * FROM read_csv_auto(?, header=true)",
            [str(Path(csv_path).resolve())],
        )

        con.execute("DROP TABLE IF EXISTS mcc_labels")
        con.execute(
            "CREATE TABLE mcc_labels(mcc INTEGER, category VARCHAR)"
        )
        con.executemany(
            "INSERT INTO mcc_labels VALUES (?, ?)",
            list(MCC_TO_CATEGORY.items()),
        )

        count, spend, earliest, latest = con.execute(
            "SELECT count(*), round(sum(amount), 2), "
            "min(posting_date), max(posting_date) FROM transactions"
        ).fetchone()

        print(
            f"Transactions: {count} rows, GBP {spend:,.2f}, "
            f"{earliest} -> {latest}"
        )

        monthly = con.execute(
            "SELECT strftime(posting_date, '%Y-%m') AS month, "
            "count(*) AS rows, round(sum(amount)) AS spend "
            "FROM transactions GROUP BY 1 ORDER BY 1"
        ).df()
        print(monthly.to_string(index=False))
        print(f"\nDatabase saved to: {db_file}")
    finally:
        con.close()


# =============================================================================
# 8. COMMAND-LINE INTERFACE
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load, categorise, evaluate and store GPC transactions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load",
        help="Load, clean and deduplicate monthly CSV files.",
    )
    load_parser.add_argument("--raw-dir", default=RAW_DIR)
    load_parser.add_argument("--output", default=CLEAN_CSV)

    categorize_parser = subparsers.add_parser(
        "categorize",
        help="Categorise transactions and evaluate accuracy.",
    )
    categorize_parser.add_argument(
        "--backend",
        default="rules",
        choices=["rules", "llm"],
    )
    categorize_parser.add_argument("--data", default=CLEAN_CSV)
    categorize_parser.add_argument("--output", default=None)

    database_parser = subparsers.add_parser(
        "build-db",
        help="Build the DuckDB database.",
    )
    database_parser.add_argument("--data", default=CLEAN_CSV)
    database_parser.add_argument("--database", default=DB_PATH)

    pipeline_parser = subparsers.add_parser(
        "pipeline",
        help="Run loading, categorisation and database creation in sequence.",
    )
    pipeline_parser.add_argument("--raw-dir", default=RAW_DIR)
    pipeline_parser.add_argument("--clean-output", default=CLEAN_CSV)
    pipeline_parser.add_argument(
        "--backend",
        default="rules",
        choices=["rules", "llm"],
    )
    pipeline_parser.add_argument("--database", default=DB_PATH)

    args = parser.parse_args()

    if args.command == "load":
        load_transactions(args.raw_dir, args.output)

    elif args.command == "categorize":
        result = categorize_file(args.data, args.backend)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(args.output, index=False)
            print(f"\nPredictions saved to: {args.output}")

    elif args.command == "build-db":
        build_database(args.data, args.database)

    elif args.command == "pipeline":
        load_transactions(args.raw_dir, args.clean_output)
        categorized = categorize_file(args.clean_output, args.backend)

        # Include predictions in the CSV consumed by the dashboard/database.
        categorized.to_csv(args.clean_output, index=False)
        print(f"\nCategorised data saved to: {args.clean_output}")

        build_database(args.clean_output, args.database)


if __name__ == "__main__":
    main()

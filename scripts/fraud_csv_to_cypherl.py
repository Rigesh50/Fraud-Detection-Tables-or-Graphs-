#!/usr/bin/env python3
"""Turn the fraud CSVs into a single self-contained CYPHERL file.

The CSV loaders need the files on the database server's own filesystem, which
means a volume mount or `docker cp` and all the path trouble that comes with
them. A CYPHERL file carries the data inside the statements instead, so it
loads over stdin and needs no file access at all:

    docker exec -i <container> mgconsole < fraud_1_1m.cypherl

Statements are batched with UNWIND rather than written one CREATE per row. One
statement per row would be ~4.3 million of them: a much larger file, and slow,
since mgconsole commits each separately.

    python3 scripts/fraud_csv_to_cypherl.py --csv fraud_1_1m/csv --out fraud_1_1m.cypherl
"""

import argparse
import csv
from pathlib import Path

BATCH = 1000
TX_TYPES = ["CashIn", "CashOut", "Payment", "Transfer", "Debit"]

ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def quote(value):
    return '"' + "".join(ESCAPES.get(c, c) for c in value) + '"'


def rows(path):
    with open(path, encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def emit_batches(out, items, statement):
    """Write `statement` once per BATCH-sized chunk of rendered list entries."""
    batch, total = [], 0
    for item in items:
        batch.append(item)
        if len(batch) == BATCH:
            out.write(statement.format(data="[" + ",".join(batch) + "]"))
            total += len(batch)
            batch = []
    if batch:
        out.write(statement.format(data="[" + ",".join(batch) + "]"))
        total += len(batch)
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="directory holding the CSVs")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    csv_dir = Path(args.csv)
    counts = {}

    with open(args.out, "w", encoding="utf-8") as out:
        # The data file holds nothing but data statements. Memgraph Lab wraps a
        # CYPHERL import in one explicit (multicommand) transaction, and
        # Memgraph refuses index, constraint and storage-mode statements inside
        # one -- "Index manipulation is not allowed in multicommand
        # transactions", and likewise for the other two. They go in separate
        # files, run on their own. Comments are out too: Lab executes the file
        # line by line, so a `//` line becomes an empty query and fails with
        # "mismatched input '<EOF>'". mgconsole tolerates all of this; the GUI
        # does not.

        # --- reference nodes ---
        counts["Bank"] = emit_batches(
            out,
            (f'{{id:{quote(r["id"])},name:{quote(r["name"])}}}' for r in rows(csv_dir / "banks.csv")),
            "UNWIND {data} AS n CREATE (:Bank {{id:n.id,name:n.name}});\n",
        )
        counts["Merchant"] = emit_batches(
            out,
            (f'{{id:{quote(r["id"])},name:{quote(r["name"])}}}'
             for r in rows(csv_dir / "merchants.csv")),
            "UNWIND {data} AS n CREATE (:Merchant {{id:n.id,name:n.name}});\n",
        )
        counts["SSN"] = emit_batches(
            out,
            (quote(r["ssn"]) for r in rows(csv_dir / "ssns.csv")),
            "UNWIND {data} AS n CREATE (:SSN {{ssn:n}});\n",
        )
        counts["Email"] = emit_batches(
            out,
            (quote(r["email"]) for r in rows(csv_dir / "emails.csv")),
            "UNWIND {data} AS n CREATE (:Email {{email:n}});\n",
        )
        counts["Phone"] = emit_batches(
            out,
            (quote(r["phone_number"]) for r in rows(csv_dir / "phones.csv")),
            "UNWIND {data} AS n CREATE (:Phone {{phoneNumber:n}});\n",
        )

        # --- clients: :Mule is a label, so the two kinds are written separately ---
        for mule, labels in (("0", ":Client"), ("1", ":Client:Mule")):
            key = "Client:Mule" if mule == "1" else "Client"
            counts[key] = emit_batches(
                out,
                (f'{{id:{quote(r["id"])},name:{quote(r["name"])}}}'
                 for r in rows(csv_dir / "clients.csv") if r["is_mule"] == mule),
                f"UNWIND {{data}} AS n CREATE ({labels} {{{{id:n.id,name:n.name}}}});\n",
            )

        # --- transactions, one pass per type (the label cannot be a variable) ---
        for kind in TX_TYPES:
            path = csv_dir / f"transactions_{kind.lower()}.csv"
            counts[kind] = emit_batches(
                out,
                (
                    f'{{id:{quote(r["id"])},amount:{float(r["amount"])!r},'
                    f'fraud:{"true" if r["fraud"] == "1" else "false"},'
                    f'step:{int(r["step"])},globalStep:{int(r["global_step"])},'
                    f'ts:{int(r["ts"])}}}'
                    for r in rows(path)
                ),
                "UNWIND {data} AS n CREATE (:Transaction:" + kind +
                " {{id:n.id,amount:n.amount,fraud:n.fraud,step:n.step,"
                "globalStep:n.globalStep,ts:n.ts}});\n",
            )

        # --- identity relationships ---
        for rel, column, label, prop in (
            ("HAS_SSN", "ssn", "SSN", "ssn"),
            ("HAS_EMAIL", "email", "Email", "email"),
            ("HAS_PHONE", "phone_number", "Phone", "phoneNumber"),
        ):
            counts[rel] = emit_batches(
                out,
                (f'{{a:{quote(r["id"])},b:{quote(r[column])}}}'
                 for r in rows(csv_dir / "clients.csv")),
                "UNWIND {data} AS r MATCH (a:Client {{id:r.a}}), (b:" + label +
                " {{" + prop + ":r.b}}) CREATE (a)-[:" + rel + "]->(b);\n",
            )

        # --- PERFORMED and TO, per transaction type ---
        for kind in TX_TYPES:
            path = csv_dir / f"transactions_{kind.lower()}.csv"
            counts[f"PERFORMED/{kind}"] = emit_batches(
                out,
                (f'{{a:{quote(r["client_id"])},b:{quote(r["id"])}}}' for r in rows(path)),
                "UNWIND {data} AS r MATCH (a:Client {{id:r.a}}), "
                "(b:Transaction {{id:r.b}}) CREATE (a)-[:PERFORMED]->(b);\n",
            )
            if kind == "Transfer":
                column, label = "to_client_id", "Client"
            elif kind == "Debit":
                column, label = "to_bank_id", "Bank"
            else:
                column, label = "to_merchant_id", "Merchant"
            counts[f"TO/{kind}"] = emit_batches(
                out,
                (f'{{a:{quote(r["id"])},b:{quote(r[column])}}}' for r in rows(path)),
                "UNWIND {data} AS r MATCH (a:Transaction {{id:r.a}}), (b:" + label +
                " {{id:r.b}}) CREATE (a)-[:TO]->(b);\n",
            )

        # --- NEXT chains, once every transaction exists ---
        for kind in TX_TYPES:
            path = csv_dir / f"transactions_{kind.lower()}.csv"
            counts[f"NEXT/{kind}"] = emit_batches(
                out,
                (f'{{a:{quote(r["id"])},b:{quote(r["next_transaction_id"])}}}'
                 for r in rows(path) if r["next_transaction_id"]),
                "UNWIND {data} AS r MATCH (a:Transaction {{id:r.a}}), "
                "(b:Transaction {{id:r.b}}) CREATE (a)-[:NEXT]->(b);\n",
            )

        # --- FIRST_TX / LAST_TX ---
        for rel, column in (("FIRST_TX", "first_tx_id"), ("LAST_TX", "last_tx_id")):
            counts[rel] = emit_batches(
                out,
                (f'{{a:{quote(r["id"])},b:{quote(r[column])}}}'
                 for r in rows(csv_dir / "clients.csv") if r[column]),
                "UNWIND {data} AS r MATCH (a:Client {{id:r.a}}), "
                "(b:Transaction {{id:r.b}}) CREATE (a)-[:" + rel + "]->(b);\n",
            )

    out_path = Path(args.out)
    stem = out_path.name[:-len(".cypherl")] if out_path.name.endswith(".cypherl") else out_path.stem
    before = out_path.with_name(f"{stem}_1_indexes.cypher")
    after = out_path.with_name(f"{stem}_2_constraints.cypher")
    before.write_text(
        "STORAGE MODE IN_MEMORY_ANALYTICAL;\n"
        "CREATE INDEX ON :Bank(id);\n"
        "CREATE INDEX ON :Merchant(id);\n"
        "CREATE INDEX ON :SSN(ssn);\n"
        "CREATE INDEX ON :Email(email);\n"
        "CREATE INDEX ON :Phone(phoneNumber);\n"
        "CREATE INDEX ON :Client(id);\n"
        "CREATE INDEX ON :Transaction(id);\n"
        "CREATE INDEX ON :Transaction(globalStep);\n"
    )
    after.write_text(
        "STORAGE MODE IN_MEMORY_TRANSACTIONAL;\n"
        "CREATE CONSTRAINT ON (n:Bank) ASSERT n.id IS UNIQUE;\n"
        "CREATE CONSTRAINT ON (n:Merchant) ASSERT n.id IS UNIQUE;\n"
        "CREATE CONSTRAINT ON (n:SSN) ASSERT n.ssn IS UNIQUE;\n"
        "CREATE CONSTRAINT ON (n:Email) ASSERT n.email IS UNIQUE;\n"
        "CREATE CONSTRAINT ON (n:Phone) ASSERT n.phoneNumber IS UNIQUE;\n"
        "CREATE CONSTRAINT ON (n:Client) ASSERT n.id IS UNIQUE;\n"
        "CREATE CONSTRAINT ON (n:Transaction) ASSERT n.id IS UNIQUE;\n"
    )
    print(f"  {before.name} / {after.name}: run these on their own, not inside the import")

    nodes = sum(v for k, v in counts.items() if "/" not in k and not k.startswith("HAS")
                and k not in ("FIRST_TX", "LAST_TX"))
    rels = sum(v for k, v in counts.items() if "/" in k or k.startswith("HAS")
               or k in ("FIRST_TX", "LAST_TX"))
    size = Path(args.out).stat().st_size
    print(f"{args.out}: {size / 1048576:.1f} MiB")
    print(f"  nodes {nodes}, relationships {rels}")


if __name__ == "__main__":
    main()

#! /usr/bin/env python3
import sys
import sqlite3
import argparse


def read_lines(cur, fmt, sz):
    q = """CREATE TABLE IF NOT EXISTS "records" (
    "id"	INTEGER,
    """
    for i in range(0, sz):
        q += f"off_{i}	INTEGER,\n"
    q += """PRIMARY KEY("id" AUTOINCREMENT)
                    );
    """
    cur.execute(q)

    col_names = "("
    for i in range(0, sz):
        col_names += f"off_{i},"
    col_names = col_names[:-1]
    col_names += ")"

    val_names = "("
    for i in range(0, sz):
        val_names += f"?,"
    val_names = val_names[:-1]
    val_names += ")"

    for line in sys.stdin:
        try:
            # Strip newline characters and parse JSON
            byte_line = list(bytearray.fromhex(line.strip()))
            if len(byte_line) < sz:
                byte_line.extend([None] * (sz - len(byte_line)))
            # print(byte_line)
            iq = f"INSERT INTO records {col_names} VALUES {val_names};"
            # print(iq)
            cur.execute(iq, byte_line)
        except:
            print(f"Failure parsing: {line.strip()}", file=sys.stderr)

    conn.commit()


def get_val_counts_by_offset(cur, o):
    res = cur.execute(
        f"SELECT off_{o}, count(*) FROM records GROUP BY off_{o} ORDER BY count(*) ASC;"
    )
    return res.fetchall()


def get_edge_counts_by_offsets(cur, o0, o1):
    res = cur.execute(
        f"SELECT off_{o0}, off_{o1}, count(*) AS ect from records GROUP BY off_{o0}, off_{o1};"
    )
    return res.fetchall()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fmt", help="input format data [hex]", default="hex")
    parser.add_argument("sz", help="size of DAG [8]", default=8)
    args = parser.parse_args()
    with sqlite3.connect("staging.db") as conn:
        read_lines(conn.cursor(), args.fmt, int(args.sz))
        for o in range(0, int(args.sz)):
            # print(f"offset {offset}")
            val_freq = get_val_counts_by_offset(conn.cursor(), o)
            for v, vct in val_freq:
                print(v, vct)
            if o != 0:
                print(get_edge_counts_by_offsets(conn.cursor(), o - 1, o))
    conn.close()


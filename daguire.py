#! /usr/bin/env python3
import sys
import sqlite3
import argparse
import tkinter as tk
from tkinter import ttk
from tkinter.filedialog import asksaveasfilename

# Display format options for byte labels (used when drawing nodes)
def format_byte_label(val: int | None, options: dict[str, bool]) -> str:
    if val is None:
        return str(None)
    parts = []
    if options.get("decimal", True):
        parts.append(str(val))
    if options.get("hex", True):
        parts.append(f"0x{val:02X}")
    if options.get("binary", True):
        parts.append(f"{val:b}")
    if options.get("ascii", True):
        parts.append(chr(val) if 0x20 <= val <= 0x7E else "·")
    return "\n".join(parts) if parts else str(val)

class Node:
    def __init__(self, o: int, v: int | None, ct: int):
        self.offset = o
        self.val = v
        self.text = self.getrepr(v)
        self.ratio = ct
        self.color = self.getcolor(v)
        self.coordinates = (0, 0, 0, 0)

    def setcordinates(self, coordinates):
        self.coordinates = coordinates

    def getrepr(self, v):
        if v == None:
            return str(None)
        else:
            o = str(v) + "\n"
            o += f"0x{v:02X}\n"
            o += f"{v:b}\n"
            o += chr(v) + "\n"
            return o

    def getcolor(self, v):
        if v is None:
            return "#2d2d3a"
        if v == 0xFF:
            return "#3d3d4a"
        if v == 0x00:
            return "#1a1a24"
        if v < 0x20:
            return "#c45c5c"   # muted red (control chars)
        if 0x20 <= v <= 0x7F:
            return "#b8a84e"   # muted yellow (printable ASCII)
        if 0x7F < v <= 0xBF:
            return "#4a9b99"   # muted cyan
        return "#4a9b6a"       # muted green


class Dag:
    def __init__(self, conn: sqlite3.Connection, fmt="hex", sz=8):
        self.conn = conn
        self.fmt = fmt
        self.sz = sz
        self.colnames = None
        self.valnames = None
        self.initDb()
        if self.fmt == 'hex':
            self.read_lines()
        else:
            self.read_files()

    def initDb(self):
        cur = self.conn.cursor()
        q = """CREATE TABLE IF NOT EXISTS "records" (
        "id"	INTEGER,
        """
        for i in range(0, self.sz):
            q += f"off_{i}	INTEGER,\n"
        q += """PRIMARY KEY("id" AUTOINCREMENT)
                        );
        """
        cur.execute(q)
        col_names = "("
        for i in range(0, self.sz):
            col_names += f"off_{i},"
        col_names = col_names[:-1]
        col_names += ")"
        self.colnames = col_names
        val_names = "("
        for i in range(0, self.sz):
            val_names += f"?,"
        val_names = val_names[:-1]
        val_names += ")"
        self.valnames = val_names

    def read_lines(self):
        print("Reading data from STDIN")
        cur = self.conn.cursor()
        for line in sys.stdin:
            try:
                byte_line = list(bytearray.fromhex(line.strip()))[:self.sz]
                if len(byte_line) < self.sz:
                    byte_line.extend([None] * (self.sz - len(byte_line)))
                iq = f"INSERT INTO records {self.colnames} VALUES {self.valnames};"
                cur.execute(iq, byte_line)
            except:
                print(f"Failure parsing: {line.strip()}", file=sys.stderr)
        self.conn.commit()

    def read_files(self):
        print("Reading data from FILES")
        cur = self.conn.cursor()
        for path in sys.stdin:
            try:
                with open(path.strip(), 'rb') as f:
                    byte_line = list(bytearray(f.read(self.sz)))
                    if len(byte_line) < self.sz:
                        byte_line.extend([None] * (self.sz - len(byte_line)))
                    iq = f"INSERT INTO records {self.colnames} VALUES {self.valnames};"
                    cur.execute(iq, byte_line)
            except Exception as e:
                print(f"Failure parsing: {path.strip()}, {e}", file=sys.stderr)
        self.conn.commit()

    def get_val_counts_by_offset(self, o: int):
        cur = self.conn.cursor()
        res = cur.execute(
            f"SELECT off_{o}, count(*) FROM records GROUP BY off_{o} ORDER BY count(*) ASC;"
        )
        return res.fetchall()

    def get_edge_counts_by_offsets(self, o0: int, o1: int):
        cur = self.conn.cursor()
        res = cur.execute(
            f"SELECT off_{o0}, off_{o1}, count(*) AS ect from records GROUP BY off_{o0}, off_{o1};"
        )
        return res.fetchall()


class CanvasApp(tk.Tk):
    # Theme: dark, modern palette
    THEME = {
        "bg": "#0f0f14",
        "toolbar_bg": "#16161e",
        "canvas_bg": "#1a1a24",
        "node_outline": "#3d3d5c",
        "node_outline_width": 2,
        "node_text": "#e4e4e7",
        "node_text_light": "#fafafa",
        "font": ("Consolas", 10),
        "toolbar_fg": "#a0a0b0",
        "accent": "#7c3aed",
    }

    def __init__(self, dag: Dag):
        super().__init__()
        self.dag = dag
        self.xpad = 150
        self.ypad = 150
        self.theme = self.THEME.copy()
        self.display_options = {"decimal": True, "hex": True, "binary": True, "ascii": True}

        self.title("DAGUIRE")
        self.configure(bg=self.theme["bg"])
        if sys.platform == "win32":
            self.state("zoomed")
        else:
            self.wm_attributes("-zoomed", 1)

        self._setup_styles()
        self._build_toolbar()
        self.frame = tk.Frame(self, bg=self.theme["bg"])
        self.frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.frame, bg=self.theme["canvas_bg"], highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.pan_canvas)

        self.draw_dag()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Toolbar.TFrame",
            background=self.theme["toolbar_bg"],
        )
        style.configure(
            "Toolbar.TCheckbutton",
            background=self.theme["toolbar_bg"],
            foreground=self.theme["toolbar_fg"],
            font=self.theme["font"],
        )
        style.configure(
            "Toolbar.TButton",
            background=self.theme["toolbar_bg"],
            foreground=self.theme["toolbar_fg"],
            font=self.theme["font"],
        )
        style.map("Toolbar.TButton", background=[("active", self.theme["accent"])])
        style.configure(
            "Toolbar.TLabel",
            background=self.theme["toolbar_bg"],
            foreground=self.theme["toolbar_fg"],
            font=self.theme["font"],
        )

    def _build_toolbar(self):
        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(10, 8))
        toolbar.pack(fill="x")

        ttk.Button(toolbar, text="Save as PS…", style="Toolbar.TButton", command=self.save_canvas_as_ps).pack(side="left", padx=(0, 16))
        ttk.Button(toolbar, text="Fit to Canvas", style="Toolbar.TButton", command=self.fit_to_canvas).pack(side="left", padx=(0, 16))

        sep = tk.Frame(toolbar, width=1, bg=self.theme["node_outline"])
        sep.pack(side="left", fill="y", padx=8, pady=2)

        label = ttk.Label(toolbar, text="Node label:", style="Toolbar.TLabel")
        label.pack(side="left", padx=(0, 6))
        for key, label_text in [("decimal", "Dec"), ("hex", "Hex"), ("binary", "Bin"), ("ascii", "ASCII")]:
            var = tk.BooleanVar(value=self.display_options[key])
            var.trace_add("write", self._on_display_option_changed)
            self.display_options[f"_var_{key}"] = var
            cb = ttk.Checkbutton(toolbar, text=label_text, variable=var, style="Toolbar.TCheckbutton")
            cb.pack(side="left", padx=2)

    def _on_display_option_changed(self, *args):
        for k in ("decimal", "hex", "binary", "ascii"):
            var = self.display_options.get(f"_var_{k}")
            if isinstance(var, tk.BooleanVar):
                self.display_options[k] = var.get()
        self.redraw_dag()

    def redraw_dag(self):
        self.canvas.delete("all")
        self.draw_dag()

    def save_canvas_as_ps(self):
        filepath = asksaveasfilename(defaultextension=".eps", filetypes=[("PostScript files", "*.eps"), ("All Files", "*.*")])
        if not filepath:
            return
        self.update()
        self.canvas.postscript(file=filepath, colormode='color')

    def fit_to_canvas(self):
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        content_w = bbox[2] - bbox[0]
        content_h = bbox[3] - bbox[1]
        if content_w <= 0 or content_h <= 0:
            return
        margin = 0.9
        scale = min(cw / content_w, ch / content_h) * margin
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        self.canvas.scale("all", cx, cy, scale, scale)
        bbox2 = self.canvas.bbox("all")
        if not bbox2:
            return
        self.canvas.configure(scrollregion=bbox2)
        sw = bbox2[2] - bbox2[0]
        sh = bbox2[3] - bbox2[1]
        center_x = (bbox2[0] + bbox2[2]) / 2
        center_y = (bbox2[1] + bbox2[3]) / 2
        fx = max(0, min(1, (center_x - cw / 2) / sw)) if sw > 0 else 0
        fy = max(0, min(1, (center_y - ch / 2) / sh)) if sh > 0 else 0
        self.canvas.xview_moveto(fx)
        self.canvas.yview_moveto(fy)

    def on_mousewheel(self, event):
        if event.delta > 0 or event.num == 4:
            scale_factor = 1.1
        else:
            scale_factor = 0.9
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        self.canvas.scale("all", x, y, scale_factor, scale_factor)

    # Pan sensitivity: Tk multiplies scan delta by 10, so we scale coords for 1:1 feel
    PAN_GAIN = 0.1

    def on_button_press(self, event):
        self._pan_start = (event.x, event.y)
        self.canvas.scan_mark(event.x, event.y)

    def pan_canvas(self, event):
        sx, sy = self._pan_start
        # Pass coords so effective delta is (dx, dy) * PAN_GAIN; Tk then *10 → 1:1
        # scan_dragto requires integers
        x = int(sx + (event.x - sx) * self.PAN_GAIN)
        y = int(sy + (event.y - sy) * self.PAN_GAIN)
        self.canvas.scan_dragto(x, y)

    def create_round_rectangle(self, x1, y1, x2, y2, r=25, **kwargs):
        points = (
            x1 + r,
            y1,
            x1 + r,
            y1,
            x2 - r,
            y1,
            x2 - r,
            y1,
            x2,
            y1,
            x2,
            y1 + r,
            x2,
            y1 + r,
            x2,
            y2 - r,
            x2,
            y2 - r,
            x2,
            y2,
            x2 - r,
            y2,
            x2 - r,
            y2,
            x1 + r,
            y2,
            x1 + r,
            y2,
            x1,
            y2,
            x1,
            y2 - r,
            x1,
            y2 - r,
            x1,
            y1 + r,
            x1,
            y1 + r,
            x1,
            y1,
        )
        return self.canvas.create_polygon(points, **kwargs, smooth=True)

    def draw_nodes_on_canvas(self, nodes, x_offset=0):
        total_ratio = sum(node.ratio for node in nodes)
        x_position = x_offset
        y_position = 0
        for node in nodes:
            height = (node.ratio / total_ratio) * (
                (self.winfo_screenheight()/2) - 2 * self.ypad
            )
            width = 150
            x1, y1 = x_position, y_position
            x2, y2 = x_position + width, y_position + height
            if node.val is not None:
                label = format_byte_label(node.val, self.display_options)
                self.create_round_rectangle(
                    x1, y1, x2, y2, 25, fill=node.color, outline=self.theme["node_outline"], width=self.theme["node_outline_width"]
                )
                text_x = (x1 + x2) / 2
                text_y = (y1 + y2) / 2
                text_fill = self.theme["node_text_light"] if node.val == 0x00 else self.theme["node_text"]
                self.canvas.create_text(
                    text_x, text_y, text=label, fill=text_fill, anchor=tk.CENTER, font=self.theme["font"]
                )
                node.setcordinates((x1, y1, x2, y2))
                y_position += height + self.ypad

        return nodes

    def draw_edges_on_canvas(self, pnodes, nodes, edges):
        for edge in edges:
            if edge[0] != None and edge[1] != None:
                srcVal, dstVal = (edge[0], edge[1])
                srcNode = None
                for n in pnodes:
                    if n.val == srcVal:
                        srcNode = n
                        break
                dstNode = None
                for n in nodes:
                    if n.val == dstVal:
                        dstNode = n
                        break
                _, sy1, sx2, sy2 = srcNode.coordinates
                dx1, dy1, _, dy2 = dstNode.coordinates
                self.canvas.create_line(
                    sx2, sy1 + (sy2 - sy1) / 2, dx1, dy1 + (dy2 - dy1) / 2,
                    fill=self.theme["node_text"],
                    width=2,
                    smooth=True,
                    arrow=tk.LAST,
                )

    def draw_dag(self):
        prevOffsetNodes = []
        x_offset = 0
        for o in range(0, self.dag.sz):
            nodes = []
            val_freq = self.dag.get_val_counts_by_offset(o)
            for v, vct in val_freq:
                nodes.append(Node(o, v, vct))
            nodes = self.draw_nodes_on_canvas(nodes, x_offset)
            if o != 0:
                edges = self.dag.get_edge_counts_by_offsets(o - 1, o)
                self.draw_edges_on_canvas(prevOffsetNodes, nodes, edges)
            x_offset += self.xpad * 2
            prevOffsetNodes = nodes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fmt", help="input format data [hex, file]", default="hex")
    parser.add_argument("sz", help="size of DAG [8]", default=8)
    args = parser.parse_args()
    if int(args.sz) > 1999:
        print(f"Size limit 1999 exceeded.", file=sys.stderr)
    else:
        with sqlite3.connect(":memory:") as conn:
            d = Dag(conn, fmt=args.fmt, sz=int(args.sz))
            app = CanvasApp(d)
            app.mainloop()


if __name__ == "__main__":
    main()

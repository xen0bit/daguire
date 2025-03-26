#! /usr/bin/env python3
import sys
import sqlite3
import argparse
import tkinter as tk
from tkinter import ttk


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
            o += f"0x{v:X}\n"
            o += f"{v:b}\n"
            o += chr(v) + "\n"
            return o

    def getcolor(self, v):
        if v == None:
            return "white"
        elif v == 0xFF:
            return "white"
        elif v == 0x00:
            return "black"
        elif v < 0x20:
            # Red
            r, g, b = (251, 70, 76)
            return f"#{r:02x}{g:02x}{b:02x}"
        elif 0x20 <= v <= 0x7F:
            # Yellow
            r, g, b = (224, 222, 113)
            return f"#{r:02x}{g:02x}{b:02x}"
        elif 0x7F < v <= 0xBF:
            # Cyan
            r, g, b = (83, 223, 221)
            return f"#{r:02x}{g:02x}{b:02x}"
        else:
            # Green
            r, g, b = (68, 207, 110)
            return f"#{r:02x}{g:02x}{b:02x}"


class Dag:
    def __init__(self, conn: sqlite3.Connection, fmt="hex", sz=8):
        self.conn = conn
        self.fmt = fmt
        self.sz = sz

        self.read_lines()

    def read_lines(self, debug=False):
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

        val_names = "("
        for i in range(0, self.sz):
            val_names += f"?,"
        val_names = val_names[:-1]
        val_names += ")"

        if not debug:
            print("Reading data from STDIN")
            for line in sys.stdin:
                try:
                    # Strip newline characters and parse JSON
                    byte_line = list(bytearray.fromhex(line.strip()))[:self.sz]
                    if len(byte_line) < self.sz:
                        byte_line.extend([None] * (self.sz - len(byte_line)))
                    # print(byte_line)
                    iq = f"INSERT INTO records {col_names} VALUES {val_names};"
                    # print(iq)
                    cur.execute(iq, byte_line)
                except:
                    print(f"Failure parsing: {line.strip()}", file=sys.stderr)

        conn.commit()

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
    def __init__(self, dag: Dag):
        super().__init__()
        self.dag = dag
        self.xpad = 120
        self.ypad = 20

        self.title("Canvas Image with Scrollbars and Buttons")
        self.wm_attributes("-zoomed", 1)
        # self.geometry("600x400")

        # Create buttons to control the canvas size and color
        button_frame = tk.Frame(self)
        button_frame.pack(fill="x")

        # Button to increase width of the image canvas
        inc_width_button = ttk.Button(
            button_frame,
            text="Increase Width",
            command=lambda: self.resize_canvas(width=512),
        )
        inc_width_button.pack(side="left")

        # Button to increase height of the image canvas
        inc_height_button = ttk.Button(
            button_frame,
            text="Increase Height",
            command=lambda: self.resize_canvas(height=512),
        )
        inc_height_button.pack(side="left")

        # Button to change color filling the image canvas
        change_color_button = ttk.Button(
            button_frame, text="Change Color", command=self.change_color
        )
        change_color_button.pack(side="left")

        # Create a frame to hold the canvas and scrollbars
        self.frame = tk.Frame(self)
        self.frame.pack(fill="both", expand=True)

        # Set up the canvas
        self.canvas = tk.Canvas(self.frame, bg="#1e1e1e")
        self.canvas.pack(side="left", fill="both", expand=True)

        # Bind mouse wheel event for zooming
        # Generic
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        # Linux
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)

        # Bind click for panning
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.pan_canvas)

        # Initial size of the canvas
        self.width = 200
        self.height = 150

        # Create a rectangle to fill the canvas with color
        # self.rect = self.canvas.create_rectangle(
        #     0, 0, self.width, self.height, fill="red"
        # )

        # Draw
        self.draw_dag()

    def resize_canvas(self, width=0, height=0):
        new_width = self.width + width
        new_height = self.height + height
        self.canvas.coords(self.rect, 0, 0, new_width, new_height)

        # Update dimensions
        self.width = new_width
        self.height = new_height

        # Configure the scroll region to fit the canvas content
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def change_color(self):
        current_color = self.canvas.itemcget(self.rect, "fill")
        if current_color == "red":
            self.canvas.itemconfig(self.rect, fill="blue")
        elif current_color == "blue":
            self.canvas.itemconfig(self.rect, fill="green")
        else:
            self.canvas.itemconfig(self.rect, fill="red")

    def on_mousewheel(self, event):
        # Get the scroll direction
        if event.delta > 0 or event.num == 4:
            scale_factor = 1.1  # Zoom in
        else:
            scale_factor = 0.9  # Zoom out

        # Get current position of mouse pointer
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)

        # Scale the canvas items around the mouse pointer position
        self.canvas.scale("all", x, y, scale_factor, scale_factor)

        # Configure the scroll region to fit the resized content
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_button_press(self, event):
        # Store the last known position of the mouse.
        self.panx, self.pany = event.x, event.y

    def pan_canvas(self, event):
        # Calculate the difference between the current and last positions of the mouse.
        dx = event.x - self.panx
        dy = event.y - self.pany

        # Update the position of the canvas by moving it by the calculated difference.
        self.canvas.scan_dragto(dx, dy, gain=1)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)
        if self.fullscreen:
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}")
        else:
            self.geometry("600x400")

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
        canvas_width = 200
        total_ratio = sum(node.ratio for node in nodes)
        canvas_height = sum(
            (node.ratio / total_ratio) * (self.winfo_screenheight() - 2 * self.ypad)
            for node in nodes
        ) + self.ypad * (len(nodes) + 1)

        x_position = x_offset
        y_position = 0
        for node in nodes:
            height = (node.ratio / total_ratio) * (
                self.winfo_screenheight() - 2 * self.ypad
            )
            width = 150

            x1, y1 = x_position, y_position
            x2, y2 = x_position + width, y_position + height

            # Draw the rounded rectangle for the node
            if node.text != "None":
                self.create_round_rectangle(
                    x1, y1, x2, y2, 25, fill=node.color, outline="white", width=3
                )

                # Calculate text position and add it to the node
                text_x = (x1 + x2) / 2
                text_y = (y1 + y2) / 2
                self.canvas.create_text(
                    text_x, text_y, text=node.text, anchor=tk.CENTER
                )

                node.setcordinates((x1, y1, x2, y2))

                # Update the y_position for the next node
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

                sx1, sy1, sx2, sy2 = srcNode.coordinates
                dx1, dy1, dx2, dy2 = dstNode.coordinates

                # print(sx1, sy1, sx2, sy2)
                # print(dx1, dy1, dx2, dy2)

                self.canvas.create_line(
                    sx2, sy1 + (sy2 - sy1) / 2, dx1, dy1 + (dy2 - dy1) / 2,
                    fill='white',
                    width=3,
                    smooth=True,
                    arrow=tk.LAST
                )

    def draw_dag(self):
        prevOffsetNodes = []
        x_offset = 0
        for o in range(0, self.dag.sz):
            print(f"offset {o}")
            # Draw Nodes
            nodes = []
            val_freq = self.dag.get_val_counts_by_offset(o)
            for v, vct in val_freq:
                nodes.append(Node(o, v, vct))
            nodes = self.draw_nodes_on_canvas(nodes, x_offset)
            # Draw Edges
            if o != 0:
                edges = self.dag.get_edge_counts_by_offsets(o - 1, o)
                self.draw_edges_on_canvas(prevOffsetNodes, nodes, edges)

            x_offset += self.xpad * 2

            prevOffsetNodes = nodes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("fmt", help="input format data [hex]", default="hex")
    parser.add_argument("sz", help="size of DAG [8]", default=8)
    args = parser.parse_args()
    with sqlite3.connect("staging.db") as conn:
        d = Dag(conn, fmt=args.fmt, sz=int(args.sz))
        app = CanvasApp(d)
        app.mainloop()

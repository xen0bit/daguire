#! /usr/bin/env python3
import sys
import sqlite3
import argparse
import tkinter as tk
from tkinter import ttk


class Node:
    def __init__(self, o: int, v: int | None, ct: int):
        self.offset = o
        self.text = self.getrepr(v)
        self.ratio = ct
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
                    byte_line = list(bytearray.fromhex(line.strip()))
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
        self.canvas = tk.Canvas(self.frame, bg="white")
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

    def draw_nodes_on_canvas(self, nodes, x_offset=0, padding=10):
        canvas_width = 200
        total_ratio = sum(node.ratio for node in nodes)
        canvas_height = sum(
            (node.ratio / total_ratio) * (self.winfo_screenheight() - 2 * padding)
            for node in nodes
        ) + padding * (len(nodes) + 1)

        # canvas = tk.Canvas(root, width=canvas_width, height=canvas_height, bg="white")
        # self.canvas.pack(pady=padding)

        x_position = x_offset
        y_position = 0
        coordinates = []

        for node in nodes:
            height = (node.ratio / total_ratio) * (
                self.winfo_screenheight() - 2 * padding
            )
            width = 150
            x1, y1 = x_position, y_position
            x2, y2 = x_position + width, y_position + height

            # Draw the rectangle for the node
            if node.text != 'None':
                self.canvas.create_rectangle(x1, y1, x2, y2, fill="lightblue")
                # Calculate text position and add it to the node
                text_x = (x1 + x2) / 2
                text_y = (y1 + y2) / 2
                self.canvas.create_text(text_x, text_y, text=node.text, anchor=tk.CENTER)

                node.setcordinates((x1, y1, x2, y2))

            # Update the y_position for the next node
            y_position += height + padding

        return nodes

    def draw_dag(self, vertical_padding=20, horizontal_padding=40):
        x_offset = 0
        for o in range(0, self.dag.sz):
            print(f"offset {o}")
            # Draw Nodes
            nodes = []
            val_freq = self.dag.get_val_counts_by_offset(o)
            for v, vct in val_freq:
                nodes.append(Node(o, v, vct))
            nodes = self.draw_nodes_on_canvas(nodes, x_offset)
            for node in nodes:
                print(node.coordinates)
            # Draw Edges
            if o != 0:
                print(self.dag.get_edge_counts_by_offsets(o - 1, o))

            x_offset += 150 + horizontal_padding


if __name__ == "__main__":
    # parser = argparse.ArgumentParser()
    # parser.add_argument("fmt", help="input format data [hex]", default="hex")
    # parser.add_argument("sz", help="size of DAG [8]", default=8)
    # args = parser.parse_args()
    with sqlite3.connect("staging.db") as conn:
        d = Dag(conn)
        app = CanvasApp(d)
        app.mainloop()

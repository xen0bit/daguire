import tkinter as tk

class Node:
    def __init__(self, text, ratio):
        self.text = text
        self.ratio = ratio

def draw_nodes_on_canvas(nodes, padding=10):
    root = tk.Tk()
    root.title("Nodes on Canvas")

    canvas_width = 200
    total_ratio = sum(node.ratio for node in nodes)
    canvas_height = sum((node.ratio / total_ratio) * (root.winfo_screenheight() - 2*padding) for node in nodes) + padding * (len(nodes) + 1)

    canvas = tk.Canvas(root, width=canvas_width, height=canvas_height, bg="white")
    canvas.pack(pady=padding)

    y_position = padding
    coordinates = []

    for node in nodes:
        height = (node.ratio / total_ratio) * (root.winfo_screenheight() - 2*padding)
        x1, y1 = 0, y_position
        x2, y2 = canvas_width, y_position + height

        # Draw the rectangle for the node
        rect_id = canvas.create_rectangle(x1, y1, x2, y2, fill="lightblue")
        # Calculate text position and add it to the node
        text_x = (x1 + x2) / 2
        text_y = (y1 + y2) / 2
        canvas.create_text(text_x, text_y, text=node.text, anchor=tk.CENTER)

        coordinates.append((rect_id, (x1, y1, x2, y2)))

        # Update the y_position for the next node
        y_position += height + padding

    root.mainloop()

    return coordinates

# Example usage:
nodes = [
    Node("Node\n 1", 1),
    Node("Node\n 2", 2),
    Node("Node\n 3", 3)
]

coords = draw_nodes_on_canvas(nodes, padding=20)
print(coords)
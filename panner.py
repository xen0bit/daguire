import tkinter as tk

class CanvasPanner(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Canvas Panner")
        self.geometry("800x600")

        # Create a canvas widget and set its background color to white.
        self.canvas = tk.Canvas(self, bg="white", width=800, height=600)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Bind the mouse events to functions
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.pan_canvas)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)

        # Initialize the last known position of the mouse.
        self.last_x, self.last_y = 0, 0

        # Add some content to the canvas for testing purposes
        self.canvas.create_rectangle(50, 50, 200, 200, fill="blue")
        self.canvas.create_oval(300, 100, 450, 250, fill="red")

    def on_button_press(self, event):
        # Store the last known position of the mouse.
        self.last_x, self.last_y = event.x, event.y

    def pan_canvas(self, event):
        # Calculate the difference between the current and last positions of the mouse.
        dx = event.x - self.last_x
        dy = event.y - self.last_y

        # Update the position of the canvas by moving it by the calculated difference.
        self.canvas.scan_dragto(event.x, event.y, gain=1)

        # Store the new known position of the mouse.
        self.last_x, self.last_y = event.x, event.y

    def on_button_release(self, event):
        pass  # No action needed on button release.

if __name__ == "__main__":
    app = CanvasPanner()
    app.mainloop()
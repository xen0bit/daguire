import tkinter as tk
from tkinter import ttk

class CanvasApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Canvas Image with Scrollbars and Buttons")
        self.geometry("600x400")

        # Create a frame to hold the canvas and scrollbars
        self.frame = tk.Frame(self)
        self.frame.pack(fill="both", expand=True)

        # Set up the canvas
        self.canvas = tk.Canvas(self.frame, bg='red')
        self.canvas.pack(side="left", fill="both", expand=True)

        # Add horizontal scrollbar to the canvas
        self.h_scrollbar = ttk.Scrollbar(self.frame, orient="horizontal", command=self.canvas.xview)
        self.h_scrollbar.pack(side="bottom", fill="x")

        # Add vertical scrollbar to the canvas
        self.v_scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        self.v_scrollbar.pack(side="right", fill="y")

        # Configure the canvas scrollbars
        self.canvas.configure(xscrollcommand=self.h_scrollbar.set,
                              yscrollcommand=self.v_scrollbar.set)

        # Create buttons to control the canvas size and color
        button_frame = tk.Frame(self)
        button_frame.pack(fill='x')

        # Button to increase width of the image canvas
        inc_width_button = ttk.Button(button_frame, text="Increase Width", command=lambda: self.resize_canvas(width=512))
        inc_width_button.pack(side="left")

        # Button to increase height of the image canvas
        inc_height_button = ttk.Button(button_frame, text="Increase Height", command=lambda: self.resize_canvas(height=512))
        inc_height_button.pack(side="left")

        # Button to change color filling the image canvas
        change_color_button = ttk.Button(button_frame, text="Change Color", command=self.change_color)
        change_color_button.pack(side="left")

        # Bind mouse wheel event for zooming
        #Generic
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        #Linux
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)

        # Initial size of the canvas
        self.width = 200
        self.height = 150

        # Create a rectangle to fill the canvas with color
        self.rect = self.canvas.create_rectangle(0, 0, self.width, self.height, fill='red')

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
        if current_color == 'red':
            self.canvas.itemconfig(self.rect, fill='blue')
        elif current_color == 'blue':
            self.canvas.itemconfig(self.rect, fill='green')
        else:
            self.canvas.itemconfig(self.rect, fill='red')

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

if __name__ == '__main__':
    app = CanvasApp()
    app.mainloop()
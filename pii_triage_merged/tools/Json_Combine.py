import json
import tkinter as tk
from tkinter import filedialog, messagebox


def combine_json_files():
    root = tk.Tk()
    root.withdraw()

    # Select input files
    file_paths = filedialog.askopenfilenames(
        title="Select JSON Files",
        filetypes=[("JSON Files", "*.json")]
    )

    if not file_paths:
        messagebox.showwarning("Warning", "No files selected.")
        return

    combined_data = {}

    try:
        for file_path in file_paths:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Case 1: Root is a dictionary
            if isinstance(data, dict):
                for key, value in data.items():
                    if key in combined_data:
                        print(f"Warning: Duplicate key '{key}' found in {file_path}. Overwriting previous value.")
                    combined_data[key] = value

            # Case 2: Root is a list of dictionaries
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        for key, value in item.items():
                            if key in combined_data:
                                print(f"Warning: Duplicate key '{key}' found in {file_path}. Overwriting previous value.")
                            combined_data[key] = value
                    else:
                        print(f"Skipping non-dictionary item in {file_path}")

            else:
                print(f"Skipping unsupported JSON structure in {file_path}")

        # Choose save location
        output_file = filedialog.asksaveasfilename(
            title="Save Combined JSON As",
            defaultextension=".json",
            initialfile="Combined.json",
            filetypes=[("JSON Files", "*.json")]
        )

        if not output_file:
            return

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(combined_data, f, indent=4, ensure_ascii=False)

        messagebox.showinfo(
            "Success",
            f"Combined {len(file_paths)} files successfully.\n\nSaved to:\n{output_file}"
        )

    except Exception as e:
        messagebox.showerror("Error", str(e))


if __name__ == "__main__":
    combine_json_files()
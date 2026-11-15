#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Edit existing Classic conventional P25 channels in a flat codeplug file."""

from dataclasses import dataclass
from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


# Open-firmware CPS image offsets (the DM-32UV wire base is not part of the file).
IMAGE_END = 0xAEE60
CHANNEL_SIZE = 56
CHANNELS_PER_BANK = 128
MODE = 0x18
TX_NAC = 0x22
LOCAL_FLAGS = 0x26
RADIO_ID = 0x27
NAC_LOW = 0x2B
NAC_HIGH = 0x2C
TALKGROUP = 0x2E
FLAGS = 0x33
P25_MODE = 0xA5
P25_TAG = 0xD0
SPLIT_NAC_TAG = 0xE0
HAS_ID = 0x80
RX_ONLY = 0x04
WIDE = 0x02


@dataclass(frozen=True)
class Fields:
    nac: int
    talkgroup: int
    radio_id: int
    tx_nac: int | None = None

    @classmethod
    def parse(cls, nac, talkgroup, radio_id, tx_nac=""):
        if not re.fullmatch(r"(?:0x)?[0-9a-f]{1,3}", nac.strip(), re.IGNORECASE):
            raise ValueError("Enter a hexadecimal NAC between 000 and FFF.")
        if not re.fullmatch(r"[0-9]{1,5}", talkgroup.strip()):
            raise ValueError("Enter a decimal talkgroup between 0 and 65535.")
        if not re.fullmatch(r"[0-9]{1,7}", radio_id.strip()):
            raise ValueError("Enter a decimal radio ID between 0 and 9999999.")
        if tx_nac.strip() and not re.fullmatch(r"(?:0x)?[0-9a-f]{1,3}", tx_nac.strip(), re.IGNORECASE):
            raise ValueError("Enter a hexadecimal TX NAC or leave it blank to follow RX.")
        return cls(int(nac, 16), int(talkgroup), int(radio_id),
                   int(tx_nac, 16) if tx_nac.strip() else None)

    def validate(self, rx_only):
        if not 0 <= self.nac <= 0xFFF or self.nac == 0xF7F:
            raise ValueError("Classic r7 does not support this NAC.")
        if not 0 <= self.talkgroup <= 65535 or not 0 <= self.radio_id <= 9999999:
            raise ValueError("Talkgroup must be 0–65535 and radio ID must be 0–9999999.")
        if self.tx_nac is not None and not (
            0 <= self.tx_nac <= 0xFFF and self.tx_nac not in (0xF7E, 0xF7F)
        ):
            raise ValueError("Enter a concrete TX NAC. F7E and F7F cannot be transmitted.")
        if not rx_only:
            if self.nac == 0xF7E and self.tx_nac is None:
                raise ValueError("RX NAC F7E requires a concrete TX NAC on an RX/TX channel.")
            if not self.talkgroup or not self.radio_id:
                raise ValueError("Talkgroup 0 and radio ID 0 require an RX-only channel.")


@dataclass(frozen=True)
class Channel:
    number: int
    name: str
    rx_only: bool
    fields: Fields


class Codeplug:
    def __init__(self, data):
        if len(data) < IMAGE_END or data.startswith((b"PK\x03\x04", b"OutSecurityBin")):
            raise ValueError("Open a flat Classic r7 codeplug .bin file.")
        self._data = bytearray(data)
        self._offsets = {}
        for bank in range(8):
            bitmap = 0x3780 if bank == 0 else 0x9B1B0 + (bank - 1) * 0x1C10
            for slot in range(CHANNELS_PER_BANK):
                if not self._data[bitmap + slot // 8] & (1 << (slot % 8)):
                    continue
                offset = bitmap + 16 + slot * CHANNEL_SIZE
                record = self._data[offset:offset + CHANNEL_SIZE]
                if record[MODE] != P25_MODE or (record[NAC_HIGH] & 0xF0) not in (P25_TAG, SPLIT_NAC_TAG):
                    continue
                if not record[FLAGS] & WIDE and all(
                    self._valid_frequency(record[start:start + 4]) for start in (0x10, 0x14)
                ):
                    self._offsets[bank * CHANNELS_PER_BANK + slot + 1] = offset
        if not self._offsets:
            raise ValueError("No saved Classic P25 channels found. Create a P25 channel on the radio and read the codeplug again.")

    @staticmethod
    def _valid_frequency(bcd):
        return any(bcd) and all((byte >> 4) <= 9 and (byte & 15) <= 9 for byte in bcd)

    def channel(self, number):
        offset = self._offsets[number]
        r = self._data[offset:offset + CHANNEL_SIZE]
        name = re.split(b"[\x00\xff]", r[:16], maxsplit=1)[0].decode("latin-1")
        radio_id = int.from_bytes(r[RADIO_ID:RADIO_ID + 3], "big") if r[LOCAL_FLAGS] & HAS_ID else 0
        tx_nac = int.from_bytes(r[TX_NAC:TX_NAC + 2], "little") if (r[NAC_HIGH] & 0xF0) == SPLIT_NAC_TAG else None
        fields = Fields(((r[NAC_HIGH] & 15) << 8) | r[NAC_LOW],
                        int.from_bytes(r[TALKGROUP:TALKGROUP + 2], "little"), radio_id, tx_nac)
        return Channel(number, name, bool(r[FLAGS] & RX_ONLY), fields)

    def channels(self):
        return (self.channel(number) for number in self._offsets)

    def edit(self, number, fields):
        channel = self.channel(number)
        fields.validate(channel.rx_only)
        offset = self._offsets[number]
        if fields == channel.fields:
            return False
        self._data[offset + NAC_LOW] = fields.nac & 0xFF
        split = fields.tx_nac is not None
        self._data[offset + NAC_HIGH] = (SPLIT_NAC_TAG if split else P25_TAG) | (fields.nac >> 8)
        if split:
            self._data[offset + TX_NAC:offset + TX_NAC + 2] = fields.tx_nac.to_bytes(2, "little")
        elif channel.fields.tx_nac is not None:
            self._data[offset + TX_NAC:offset + TX_NAC + 2] = b"\xff\xff"
        self._data[offset + TALKGROUP:offset + TALKGROUP + 2] = fields.talkgroup.to_bytes(2, "little")
        if fields.radio_id != channel.fields.radio_id:
            if fields.radio_id:
                self._data[offset + LOCAL_FLAGS] |= HAS_ID
                self._data[offset + RADIO_ID:offset + RADIO_ID + 3] = fields.radio_id.to_bytes(3, "big")
            else:
                self._data[offset + LOCAL_FLAGS] &= ~HAS_ID
                self._data[offset + RADIO_ID:offset + RADIO_ID + 3] = b"\x00\x16\x00"
        return True

    def save_copy(self, path):
        with Path(path).open("xb") as output:
            output.write(self._data)


class EditChannel(simpledialog.Dialog):
    def __init__(self, parent, channel):
        self.channel = channel
        super().__init__(parent, title=f"Edit channel {channel.number} · {channel.name}")

    def body(self, frame):
        fields = self.channel.fields
        self.entries = []
        for row, (label, value) in enumerate((
            ("RX NAC (hex)", f"{fields.nac:03X}"),
            ("Talkgroup", str(fields.talkgroup)),
            ("Radio ID", str(fields.radio_id)),
            ("TX NAC (hex, blank follows RX)", "" if fields.tx_nac is None else f"{fields.tx_nac:03X}"),
        )):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
            entry = ttk.Entry(frame, width=16)
            entry.insert(0, value)
            entry.grid(row=row, column=1, padx=8, pady=6)
            self.entries.append(entry)
        ttk.Label(frame, text="RX NAC F7E accepts any receive NAC. Explicit TX NAC needs updated firmware.").grid(
            row=4, columnspan=2, sticky="w", padx=8, pady=6
        )
        self.entries[0].selection_range(0, "end")
        return self.entries[0]

    def validate(self):
        try:
            fields = Fields.parse(*(entry.get() for entry in self.entries))
            fields.validate(self.channel.rx_only)
        except ValueError as error:
            messagebox.showerror("Check channel settings", str(error), parent=self)
            return False
        self.result = fields
        return True


class Editor:
    def __init__(self, root):
        self.root = root
        self.codeplug = None
        self.source = None
        self.dirty = False
        root.title("Classic P25 Codeplug Editor")
        root.geometry("730x440")
        root.minsize(680, 300)
        root.protocol("WM_DELETE_WINDOW", self.close)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Button(toolbar, text="Open codeplug…", command=self.open).pack(side="left")
        self.edit_button = ttk.Button(toolbar, text="Edit channel…", command=self.edit, state="disabled")
        self.edit_button.pack(side="left", padx=8)
        self.save_button = ttk.Button(toolbar, text="Save a copy…", command=self.save, state="disabled")
        self.save_button.pack(side="right")

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        columns = ("slot", "name", "rx_nac", "tx_nac", "talkgroup", "id", "access")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in zip(columns, ("Slot", "Name", "RX NAC", "TX NAC", "Talkgroup", "Radio ID", "Access"),
                                     (52, 145, 72, 90, 80, 95, 75)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=width, stretch=(key == "name"))
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.table.pack(side="left", fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.selection_changed)
        self.table.bind("<Return>", lambda event: self.edit())
        self.table.bind("<Double-1>", lambda event: self.edit() if self.table.identify_row(event.y) else None)
        self.status = tk.StringVar(value="Open a flat Classic r7 codeplug (.bin) to edit saved P25 channels.")
        ttk.Label(frame, textvariable=self.status).pack(anchor="w", pady=(10, 0))

    def selection_changed(self, _event=None):
        self.edit_button.configure(state="normal" if self.table.selection() else "disabled")

    def can_discard(self):
        return not self.dirty or messagebox.askyesno(
            "Discard edits?", "Discard changes since the last saved copy?", parent=self.root
        )

    def close(self):
        if self.can_discard():
            self.root.destroy()

    @staticmethod
    def row(channel):
        fields = channel.fields
        tx = "Same as RX" if fields.tx_nac is None else f"{fields.tx_nac:03X}"
        return (channel.number, channel.name, f"{fields.nac:03X}", tx, fields.talkgroup, fields.radio_id,
                "RX only" if channel.rx_only else "RX/TX")

    def open(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("Codeplug binary", "*.bin"), ("All files", "*")])
        if not path:
            return
        try:
            source = Path(path)
            codeplug = Codeplug(source.read_bytes())
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not open codeplug", str(error), parent=self.root)
            return
        if not self.can_discard():
            return
        self.codeplug, self.source, self.dirty = codeplug, source, False
        for item in self.table.get_children():
            self.table.delete(item)
        for channel in codeplug.channels():
            self.table.insert("", "end", iid=str(channel.number), values=self.row(channel))
        self.selection_changed()
        self.save_button.configure(state="normal")
        self.status.set(f"{source.name} · {len(self.table.get_children())} P25 channels")

    def edit(self):
        selected = self.table.selection()
        if not selected:
            return
        number = int(selected[0])
        fields = EditChannel(self.root, self.codeplug.channel(number)).result
        if fields is not None and self.codeplug.edit(number, fields):
            self.dirty = True
            self.table.item(selected[0], values=self.row(self.codeplug.channel(number)))
            self.status.set("Unsaved edits")

    def save(self):
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".bin", filetypes=[("Codeplug binary", "*.bin")],
            initialdir=self.source.parent, initialfile=f"{self.source.stem}-p25.bin", confirmoverwrite=False,
        )
        if not path:
            return
        try:
            self.codeplug.save_copy(path)
        except FileExistsError:
            messagebox.showerror("File already exists", "Choose a new filename for the copy.", parent=self.root)
        except OSError as error:
            messagebox.showerror("Could not save codeplug", str(error), parent=self.root)
        else:
            self.dirty = False
            self.status.set(f"Saved {Path(path).name}")


if __name__ == "__main__":
    root = tk.Tk()
    Editor(root)
    root.mainloop()

# daguire

Directed Acyclic Graph (DAG)
User Interface (UI)
Reverse Engineering (RE)

This script reads in arbitrary data samples of varying lengths, performs frequency analysis for a value at a given offset, and graphs the flow diagram.

Scroll wheel on your mouse to zoom in/out. Click and hold to pan.

Values are single bytes and are graphed complete with their Decimal, Hexidecimal, Binary, and ASCII representations.
The graphed node containing the value is color coded according to byte-class:

```
0x00        : Black
0x01 - 0x20 : Red
0x21 - 0x7F : Yellow (ASCII Printable Range)
0x80 - 0xBF : Cyan
0xC0 - 0xFE : Green
0xFF        : White
```

All python3 stdlib. No need to pip install anything. That also means it's cross platform. It just works.

```
usage: daguire.py [-h] fmt sz

positional arguments:
  fmt         input format data [hex]
  sz          size of DAG [8]

options:
  -h, --help  show this help message and exit
```

# Example usage:

## Protocol reverse engineering

```bash
tshark -r sample.pcap -T fields -e data | python3 daguire.py hex 1024
```

## File format reverse engineering

```bash
find "/home/remy/firmware_downloads/" -name "vendorXproductYversion*.bin" | python3 daguire.py file 1999
```

## Other

There's a button in the top left hand corner to save the canvas as `*.eps PostScript`. Yes saving as a PNG would be nice, but that's not python stdlib so convert it yourself.


# daguire

**DAG**irected **UI** for **RE**verse **E**ngineering

Pronounced like "dagger".

Interactive DAG viewer for binary reverse engineering. Reads arbitrary data samples of varying lengths, performs frequency analysis for values at given offsets, and graphs flow diagrams.

**Input is passed through STDIN.**

## Features

- **Interactive canvas**: Scroll wheel to zoom, click and drag to pan
- **Multi-format labels**: Display decimal, hexadecimal, binary, and ASCII representations
- **Color-coded nodes**: Bytes are color-coded by value class:
  ```
  0x00        : Black
  0x01 - 0x20 : Red
  0x21 - 0x7F : Yellow (ASCII printable range)
  0x80 - 0xBF : Cyan
  0xC0 - 0xFE : Green
  0xFF        : White
  ```
- **SVG export**: Headless export mode for automation
- **Structure definitions**: Save and apply structure definitions (.dgs format)
- **Pure Python stdlib**: No external dependencies, cross-platform

## Installation

This repo is a [uv](https://docs.astral.sh/uv/) project. From the project directory:

```bash
uv run daguire hex 8
```

Or run the script directly:

```bash
uv run python daguire.py hex 8
```

Once published to PyPI, run from anywhere with:

```bash
uvx run daguire hex 8
```

## Usage

```
usage: daguire.py [-h] [--svg PATH] [--structure NAME] [--list-structures]
                  [--decimal] [--no-decimal] [--hex] [--no-hex] [--binary]
                  [--no-binary] [--ascii] [--no-ascii] [--filter-match]
                  [fmt] [sz]

positional arguments:
  fmt                input format data [hex, file]
  sz                 size of DAG [8]

options:
  -h, --help         show this help message and exit
  --svg PATH         export SVG to file and exit (no GUI)
  --structure NAME   apply saved structure by name
  --list-structures  list saved structures and exit
  --decimal          show decimal in labels (default: on)
  --no-decimal       hide decimal in labels
  --hex              show hex in labels (default: on)
  --no-hex           hide hex in labels
  --binary           show binary in labels (default: on)
  --no-binary        hide binary in labels
  --ascii            show ASCII in labels (default: on)
  --no-ascii         hide ASCII in labels
  --filter-match     filter to only records matching the structure
```

## Examples

### Protocol reverse engineering

```bash
tshark -r sample.pcap -T fields -e data | uv run daguire hex 1024
```

### File format reverse engineering

```bash
find "/path/to/firmware/" -name "*.bin" | uv run daguire file 1999
```

### Headless SVG export

```bash
cat data.hex | uv run daguire hex 16 --svg output.svg
```

### Apply saved structure

```bash
cat data.hex | uv run daguire hex 16 --structure MyHeader
```

### List saved structures

```bash
uv run daguire --list-structures
```

## Testing

Test data is available in `tests/`:

```bash
# Hex format samples
cat tests/hex_samples.txt | uv run daguire hex 8

# Binary file samples
cat tests/file_list.txt | uv run daguire file 8
```

See [tests/README.md](tests/README.md) for details.

## Export

Click the button in the top-left corner to save the canvas as `*.eps` PostScript format.


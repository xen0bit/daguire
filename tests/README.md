# Test data for daguire

## Hex format (stdin)

- **hex_samples.txt** — One hex string per line. Use with:

  ```bash
  cat tests/hex_samples.txt | uv run daguire hex 8
  ```

## File format (paths on stdin)

- **bin/** — Small binary samples:
  - `sample1.bin` — "MAGIC" + null + 0x01
  - `sample2.bin` — bytes 0x00–0x07
  - `sample3.bin` — "Hello!" + two nulls

- **file_list.txt** — Paths to the bin files (relative to project root). Use with:

  ```bash
  cat tests/file_list.txt | uv run daguire file 8
  ```

Run from the project root so the paths in `file_list.txt` resolve.

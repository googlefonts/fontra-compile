# fontra-compile — A Variable Font outline compiler for Fontra

This is for now a work in progress proof of concept.

Initial goals:

- Focus on outlines and (variable) components
- Input: Fontra "backend" objects
- Initially [glyph-1](https://github.com/harfbuzz/boring-expansion-spec/blob/main/glyf1.md)-only
- [Variable Components](https://github.com/harfbuzz/boring-expansion-spec/blob/main/glyf1-varComposites.md)
- [Cubics outlines in glyf](https://github.com/harfbuzz/boring-expansion-spec/blob/main/glyf1-cubicOutlines.md)

Future goals:

- Add option to convert cubic curves to quadratics
- Add option to convert quadratic curves to cubics
- Add option to flatten variable components
- Add option to build a backwards compatible glyf-0 table

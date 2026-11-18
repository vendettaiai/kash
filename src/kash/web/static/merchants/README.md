# Merchant logos

Brand icons from [Simple Icons](https://simpleicons.org) 16.33.0
(`cdn.jsdelivr.net/npm/simple-icons@16.33.0/icons/<slug>.svg`), licensed
[CC0 1.0](https://github.com/simple-icons/simple-icons/blob/develop/LICENSE.md).
The brand colour from Simple Icons' data is added as the SVG `fill`; very light
brand colours are drawn dark instead and used as the tile background
(see `services/merchant_icons.py`).

The logos are trademarks of their owners and are used only to identify the
merchant, as Simple Icons' disclaimer describes.

To add one: copy `<slug>.svg` from the same package into this folder, add the
`fill`, and add an entry to `CATALOG` in `src/kash/services/merchant_icons.py`.

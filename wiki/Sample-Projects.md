# Sample Projects

## [Tactics Ogre: The Knight of Lodis (USA)](https://github.com/YoriKv/celpix/tree/main/sample-project-public/gba-tactics-ogre)

[![Tactics Ogre: The Knight of Lodis (USA)](https://raw.githubusercontent.com/YoriKv/celpix/main/sample-project-public/gba-tactics-ogre/screenshot.png)](https://github.com/YoriKv/celpix/tree/main/sample-project-public/gba-tactics-ogre)

The 122 character portraits, editable and writable back to the ROM.

| Part | How it works |
| --- | --- |
| Portrait archive | One slice over the game's `A7` archive at `$086E3F3C`. A project plugin, `plugins/compression/lodis_a7.py`, unpacks its entries against the archive's preset dictionary. On save it re-packs every entry, rewrites the entry table and keeps the header and dictionary. |
| Portraits | 122 [nested slices](Compress-and-Reshape), cut from the unpacked archive. Each one is a 40×48 portrait of 30 GBA 4bpp tiles. |
| Tile order | The game stores a portrait as four sprite pieces: 32×32, 8×32, 32×16 and 8×16. A reshape plugin, `plugins/reshape/lodis_portrait.py`, arranges them into the 40×48 portrait. It puts them back in stored order on save. |
| Palettes | Each portrait reads its own BGR555 palette from the ROM, by offset. |


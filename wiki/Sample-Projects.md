# Sample Projects

## [Tactics Ogre: The Knight of Lodis (USA)](https://github.com/YoriKv/celpix/tree/main/sample-project-public/gba-tactics-ogre)

[![Tactics Ogre: The Knight of Lodis (USA)](https://raw.githubusercontent.com/YoriKv/celpix/main/sample-project-public/gba-tactics-ogre/screenshot.png)](https://github.com/YoriKv/celpix/tree/main/sample-project-public/gba-tactics-ogre)

The 122 character portraits, editable and writable back to the ROM.

The game stores each portrait twice. Battles show the 40×48 portrait, preparations
and cutscenes show a 48×48 one that's kept in a second archive.

| Part | How it works |
| --- | --- |
| Portrait archive | One slice over the game's `A7` archive at `$086E3F3C`. A project plugin, `plugins/compression/lodis_a7.py`, unpacks its entries against the archive's preset dictionary. On save it re-packs every entry, rewrites the entry table and keeps the header and dictionary. |
| Portraits | 122 [nested slices](Compress-and-Reshape), cut from the unpacked archive. Each one is a 40×48 portrait of 30 GBA 4bpp tiles. |
| 48×48 portraits | A second `A7` slice, at `$086C052C`, through the same plugin. Its tiles are already in display order, so they don't need the reshape step. It uses the same palette as the battle portrait. |
| Tile order | The game stores a 40×48 portrait as four sprite pieces: 32×32, 8×32, 32×16 and 8×16. A reshape plugin, `plugins/reshape/lodis_portrait.py`, arranges them into the 40×48 portrait. It puts them back in stored order on save. |
| Palettes | Each portrait reads its own BGR555 palette from the ROM, by offset. |


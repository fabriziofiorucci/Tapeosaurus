## Tapeosaurus: Enclosure

This repository folder contains the 3D design and print files for the Tapeosaurus enclosure. Use these files to modify, inspect, or 3D-print the enclosure that fits the Tapeosaurus electronics.

<div align="center"><img src="/img/tapeosaurus.cad.jpg" alt="Tapeosaurus enclosure"></div>

<p align="middle">
  <img src="/img/tapeosaurus-top.1.jpg" width="500" alt="Tapeosaurus enclosure top view 1" />
  <img src="/img/tapeosaurus-top.2.jpg" width="500" alt="Tapeosaurus enclosure top view 2" />
</p>
<p align="middle">
  <img src="/img/tapeosaurus-lid.jpg" width="500" alt="Tapeosaurus enclosure lid" />
  <img src="/img/tapeosaurus-top.3.jpg" width="500" alt="Tapeosaurus enclosure top view 3" />
</p>
<p align="middle">
  <img src="/img/tapeosaurus-full.jpg" width="500" alt="Tapeosaurus enclosure full" />
  <img src="/img/tapeosaurus-ports.jpg" width="500" alt="Tapeosaurus enclosure ports side" />
</p>

### Contents
- Fusion 360 source [Tapeosaurus.f3d](Tapeosaurus.f3d) — complete Fusion 360 archive with all bodies, sketches, and appearance setup.
- 3D printable parts in the [stl](stl) directory

### Recommended print settings
- Material: PLA (black and white)
- Layer height: 0.10 mm
- Perimeters: 3
- Infill: 15–20%
- Supports: main body and lid

### Customization tips
- To change the logo or button legends, open Tapeosaurus-enclosure.f3d and edit the emboss/cut features in the top body.
- To accommodate a different PCB or connector, modify standoff locations and cutouts in the Fusion 360 file, then re-export STLs.
- For better snap-fit, add chamfers or flexible hooks in Fusion and test with a single calibration print.

### License & attribution
- These CAD and print files are distributed under the license at the repository root (see LICENSE). If no license is present, ask the project owner before redistributing or reusing commercially.
- If you publish modified enclosures derived from these files, please credit the Tapeosaurus project.

### Contact / contributions
- To propose improvements, open a GitHub issue or submit a pull request with updated F3D and exported STLs.
- Include test prints and notes about printer/model/settings when submitting changes.

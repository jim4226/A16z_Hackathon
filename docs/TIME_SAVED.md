# Time saved: cited baseline

How long one revision of the robot takes a single experienced engineer **by hand** (hands-on engineer time only: no waiting, fabrication or meetings), next to Morph.
Built from published surveys, papers and case studies: **verified 38/40 figures against their sources**. Source of truth: `morph/robot/time_baseline.json`.

| Step | Low | Typical | High |
|---|---:|---:|---:|
| Requirements + spec | 4 h | **12 h** | 64 h |
| Mechanical CAD (arm + gripper) | 32 h | **56 h** | 200 h |
| Simulation setup + runs | 12 h | **24 h** | 56 h |
| Electronics: parts, schematic, PCB | 22 h | **57 h** | 122 h |
| Firmware | 16 h | **40 h** | 160 h |
| BOM + sourcing | 4 h | **10 h** | 24 h |
| Manufacturing prep + quotes | 8 h | **18 h** | 35 h |
| Assembly instructions | 12 h | **24 h** | 85 h |
| **One full revision** | 110 h | **241 h (~30 workdays)** | 746 h |

Each later design change: **24 h typical** (8–80 h).

**With Morph:** the full pipeline (CAD, simulations, electronics + PCB + firmware, manufacturing, BOM, manual) runs in seconds to about a minute. We add a stated **15 min engineer review per new design and 5 min per change**, so the comparison stays honest (~240 h vs ~15 min, ≈ 950×).

## Pitch one-liners

- An expert engineer estimated that designing just one robot-gripper fingertip by hand takes 6-8 hours, including 3D-print checks (Wolniakowski et al., ROBOVIS 2020). ([source](https://www.scitepress.org/Papers/2020/100201/100201.pdf))
- In Sandia's analysis of design-through-analysis work, running the simulation took only 4% of analysts' engineering time; about 91% went to setup such as CAD cleanup, geometry decomposition and meshing (Sandia National Laboratories, SAND2005-4647, 2005). ([source](https://www.osti.gov/servlets/purl/876325))
- Laying out even a small 2-layer circuit board took 12-40 engineer-hours, before any schematic or part selection (Bazeghi et al., IFIP/IEEE VLSI-SoC 2007). ([source](https://iacoma.cs.uiuc.edu/iacoma-papers/vlsi07.pdf))
- 83% of manufacturing leaders say their typical engineer spends 4+ hours every week on supplier sourcing, quoting and ordering (Fictiv x MISUMI State of Manufacturing Report, 2026). ([source](https://www.fictiv.com/2026-state-of-manufacturing-report))
- Handling engineering changes uses up about 31% of manufacturers' R&D capacity (Langer et al., DESIGN 2012 survey of Danish companies). ([source](https://strathprints.strath.ac.uk/79508/1/Langer_etal_DS70_2012_Exploring_differences_between_average_and_critical_engineering_changes.pdf))

## How each number was derived

### Requirements + spec
Estimate, informed by the 2019 Embedded Markets Study. No source gives hours for writing a spec for a small robot arm. Low is 4 h: a one-page requirements list with no review. Typical is 12 h (1.5 days): requirements, reach and payload numbers, joint-torque sizing to pick actuators, and a controller/architecture choice. High is 64 h. In the 2019 bars of the 'What percentage of your design time is spent on each stage?' chart (p.38, N=424), 'Developing system specs' is 13% against 31% for 'Detailed design'. I read the 13% myself from the same chart that holds the verified 31% and 19% figures; the 2019 values add to 99%. Applying that 13/31 ratio to this baseline's typical detailed-design effort (CAD 56 h + electronics 57 h + firmware 40 h = 153 h) gives about 64 h. Typical is set well below that ratio because a small team writes lighter specs than the ~16-engineer, ~12-month projects in that survey. Context: a 2006 Flomerics survey named design requirement changes (59%) as the top cause of PCB schedule overruns.

Sources: [2019 Embedded Markets Study (p.38 design-time-by-stage chart)](https://www.embedded.com/wp-content/uploads/2019/11/EETimes_Embedded_2019_Embedded_Markets_Study.pdf) (AspenCore / EETimes-Embedded.com (survey by Wilson Research Group), 2019); [Flomerics Releases PCB Design Survey Results](https://circuitsassembly.com/ca/news/293-news/2006-news/12736-flomerics-releases-pcb-design-survey-results.html) (Circuits Assembly (UP Media Group), 2006)

### Mechanical CAD (arm + gripper)
GRIPPER (low 8 h / typical 16 h / high 80 h):
- Low 8 h: Kuhn-Stoff's average design time for one custom AM gripper hand, a vendor CEO estimate for adapting finger geometry on an existing base gripper.
- Typical 16 h: two fingertips at the 6-8 h per fingertip an expert engineer reported in Wolniakowski et al. (2020), which includes 3D-print verification. Taken as 2 x 8 h, with sensor mounting absorbed into that figure.
- High 80 h: about 2 weeks (10 workdays x 8 h), Honarpardaz's figure for manually designing one industrial finger at 3-4 working days per iteration. The same researcher told LiU it takes 'at least a month' for an expert, so 80 h is the conservative end.

ARM (low 24 h / typical 40 h / high 120 h): estimate only. No verified source gives hours for CAD of a whole 5-DOF arm. Assumes about 12-20 custom parts (base, turntable, shoulder/elbow/wrist links, servo brackets, covers).
- Low: 12 parts x 2 h.
- Typical: 16 parts x 2.5 h.
- High: 20 parts x 6 h, using the 6-8 h-per-fingertip figure as the per-part ceiling for complex parts.

Totals:
- Low: 8 + 24 = 32 h.
- Typical: 16 + 40 = 56 h (about 7 workdays).
- High: 80 + 120 = 200 h.

This covers one revision only. Rework from changes is in per_change_iteration.

Sources: [Robotic Finger Design Workflow for Adaptable Industrial Assembly Tasks (Wolniakowski, Lindvig, Iversen, Kruger, Kramberger), Proc. ROBOVIS 2020, pp. 69-76](https://www.scitepress.org/Papers/2020/100201/100201.pdf) (SCITEPRESS, 2020); [Finger Design Automation for Industrial Robots: A Generic and Agile Approach (M. Honarpardaz), Linkoping Studies in Science and Technology, Dissertation No. 1917](https://www.dissertations.se/dissertation/47746412ec/) (Linkoping University, 2018); [Two fingers suffice (news item quoting M. Honarpardaz)](https://liu.se/en/news-item/tva-fingrar-racker) (Linkoping University, 2018); [paramate case study: Kuhn-Stoff custom gripper hands (corroborated by EOS case study, 11/2020)](https://paramate.trinckle.com/case_kuhnstoff.php) (trinckle 3D GmbH (vendor case study), 2020)

### Simulation setup + runs
Estimate. Published sources describe robot-model setup as manual and hard but give no hours for it. In Tola & Corke's 2023 survey, 67% of URDF users wrote URDFs by hand and 89% rated creating them medium or difficult. In Afzal et al. (2020), 34% of developers skipped simulation on a project, and 53.6% of those cited lack of time or resources.

Components:
- Model build (export meshes, inertias, joint limits, actuators, contacts): low 4 h, typical 8 h, high 24 h.
- Four test scenarios, each scripted, run and post-processed: low 2 h each, typical 4 h each, high 8 h each, so 8 / 16 / 32 h.

Totals: low 12 h, typical 24 h, high 56 h.

Compute time is not counted. In Sandia's analysis, running the simulation was 4% of analyst engineering time, with about 91% going to setup steps. Tech-Clarity found preprocessing takes 38% of simulation time, against 23% waiting for the solver.

Cross-check: the Embedded Markets Study puts the simulation stage at 6% of design time against 31% for detailed design. Applied to this baseline's 153 h of detailed design, that gives about 30 h, so the 24 h typical is conservative.

Not used as a baseline: DAA's 63 h of preprocessing for one FEA iteration, and Sandia's 317-1,798 h per analysis. Both are heavy FEA work, not MuJoCo rigid-body simulation.

Sources: [DART System Analysis, SAND2005-4647 (Hardwick, Clay, Boggs, Walsh, Larzelere, Altshuler)](https://www.osti.gov/servlets/purl/876325) (Sandia National Laboratories / US DOE OSTI, 2005); [Addressing the Bottlenecks of FEA Simulation (M. Boucher)](https://www.plm.automation.siemens.com/media/global/en/Tech-Clarity_Perspective_Simulation_tcm27-33115.pdf) (Tech-Clarity, Inc. (hosted by Siemens PLM), 2016); [Understanding URDF: A Survey Based on User Experience (Tola, Corke), IEEE CASE 2023](https://arxiv.org/abs/2302.13442) (IEEE / arXiv 2302.13442, 2023); [A Study on the Challenges of Using Robotics Simulators for Testing (Afzal, Katz, Le Goues, Timperley)](https://arxiv.org/abs/2004.07368) (Carnegie Mellon University / arXiv 2004.07368, 2020); [2019 Embedded Markets Study](https://www.embedded.com/wp-content/uploads/2019/11/EETimes_Embedded_2019_Embedded_Markets_Study.pdf) (AspenCore (survey by Wilson Research Group), 2019); [Design Automation Associates case study: Simcenter 3D (63 h preprocessing per FEA iteration, upper-bound context only)](https://resources.sw.siemens.com/en-US/case-study-design-automation-associates-simcenter) (Siemens Digital Industries Software, n.d.)

### Electronics: parts, schematic, PCB
Anchored on PCB layout hours that the original designers reported for real boards (Bazeghi et al. 2007, layout only).
- Three 2-layer boards took 12, 32 and 40 h.
- A 90-component motor driver took 21 h.
- The largest small board took 68 h.
- A 5-DOF arm controller (MCU, servo bus/motor drivers, regulators, load-cell amplifier, connectors; roughly 60-120 parts) is comparable to boards B11, B4, B8 and B9.

Layout: low 12 h (the smallest 2-layer board), typical 32 h (the median 2-layer board), high 68 h (the largest small board).

Schematic and part selection, scaled from Feranec's logged hours (95 h schematic, 50 h component research, 215 h layout):
- Schematic = 0.44 x layout. This is conservative: excluding the DDR3 routing, the ratio would be about 0.8.
- Component research = 0.23 x layout.
- Harness design is an estimate: 2 / 4 / 8 h.

Totals:
- Low: 12 + 5 + 3 + 2 = 22 h.
- Typical: 32 + 14 + 7 + 4 = 57 h.
- High: 68 + 30 + 16 + 8 = 122 h.

Calendar context, not counted here: Predictable Designs gives 2-5 weeks each for schematic and layout plus 1-4 weeks for critical part selection. The Aberdeen survey for Siemens averages about 3 months from schematic to manufacturing release. Fabrication alone is at least 24 h at JLCPCB, plus shipping.

Sources: [Estimating Design Time for System Circuits (Bazeghi, Mesa-Martinez, Greskamp, Torrellas, Renau), IFIP/IEEE VLSI-SoC 2007](https://iacoma.cs.uiuc.edu/iacoma-papers/vlsi07.pdf) (IFIP/IEEE (hosted by UIUC IACOMA), 2007); [Do you know how long schematic design and PCB layout take?](https://fedevel.com/blog/do-you-know-how-long-schematic-design-and-pcb-layout-take) (FEDEVEL (Robert Feranec), 2011); [Product Development Timeline: How Long It Takes to Develop a New Hardware Product](https://predictabledesigns.com/product-development-timeline-how-long-it-takes-to-develop-a-new-hardware-product/) (Predictable Designs (John Teel), c. 2019, updated); [Are you normal? (summarizing Aberdeen 'PCB Design Best Practices in 2023')](https://blogs.sw.siemens.com/electronic-systems-design/2024/03/05/are-you-normal/) (Siemens Digital Industries Software blog, 2024); [PCB Fabrication Services and Production Time](https://jlcpcb.com/help/article/pcb-fabrication-services-and-production-time) (JLCPCB, 2026)

### Firmware
Estimate, informed by two sources. Predictable Designs puts firmware/software at 1-6 months of calendar time for a commercial hardware product. The 2019 Embedded Markets Study splits design time into 19% testing and debugging versus 31% detailed design, so debugging is about 0.6x the design effort.
- Typical 40 h: about 25 h writing code plus about 15 h test and debug (0.61 x 25).
- Low 16 h: reuse the vendor servo library; about 10 h writing plus 6 h debug.
- High 160 h: the low end of Predictable Designs' range, treating 1 calendar month as about 4 x 40 h of full-time effort.

The Embedded study's 12.2-month average project (teams of about 16 engineers) is not used, because it would overstate the scope.

Sources: [Product Development Timeline: How Long It Takes to Develop a New Hardware Product](https://predictabledesigns.com/product-development-timeline-how-long-it-takes-to-develop-a-new-hardware-product/) (Predictable Designs (John Teel), c. 2019, updated); [2019 Embedded Markets Study](https://www.embedded.com/wp-content/uploads/2019/11/EETimes_Embedded_2019_Embedded_Markets_Study.pdf) (AspenCore (survey by Wilson Research Group), 2019)

### BOM + sourcing
Assumes a BOM of about 60-150 lines: servos, bearings, fasteners, shafts and electronics. Electronic component research is already counted under electronics.

Pricing, anchored on the CalcuQuote/Phase Electronics case (about 2 h to price a 100-line BOM by hand; a variant says 2-3 h):
- Low: 60 lines, 1.2 h.
- Typical: 100 lines, 2 h.
- High: 150 lines at 3 h per 100, 4.5 h.

Searching for standard and supplier parts and their CAD models: 2 / 5 / 12 h. In CADENAS's 2022 survey, 52% reported 30 min/day and 48% reported 1 h/day or more, which implies a mean of about 0.9 h/day. Only part of that is counted here, to avoid double-counting with CAD.

Compiling the BOM, alternates and vendor carts: 1 / 3 / 7.5 h.

Totals: about 4 / 10 / 24 h.

Context, not added: in Fictiv x MISUMI 2026, 83% of 321 director-level respondents said their typical engineer spends 4+ hours a week on sourcing, quoting and ordering. In Accuris 2026, 77% of 439 respondents said they spend 5+ hours a week reading datasheets and comparing alternatives. In Tech-Clarity 2021, searching for information such as specs, BOMs and supplier data was 23% of non-value-added time, or about 7.6% of all engineering time.

Sources: [Phase Electronics customer story (formerly CalcuQuote case study)](https://www.elisaindustriq.com/knowledge-center/customer-stories/phase-electronics) (Elisa IndustrIQ (formerly CalcuQuote), vendor case study, 2025); [6 Opportunities to Kick your Engineering into High Gear (2022 Engineering Efficiency Report)](https://fs.hubspotusercontent00.net/hubfs/28407/2022%20Engineering%20Efficiency%20Report.pdf) (CADENAS GmbH / CADENAS PARTsolutions, 2022); [2026 State of Manufacturing & Supply Chain Report](https://www.fictiv.com/2026-state-of-manufacturing-report) (Fictiv x MISUMI (survey by Dimensional Research), 2026); [The Hidden Tax on Engineering Teams (Fuld & Company survey for Accuris, March 2026)](https://accuristech.com/blog/the-hidden-tax-on-engineering-teams-what-inaction-on-parts-intelligence-actually-costs/) (Accuris, 2026); [How-To Reduce Non-Value-Added Work in Engineering (eBook)](https://www.solidworks.com/sites/default/files/2021-05/Tech-Clarity-ebook-NVA.pdf) (Tech-Clarity (licensed to Dassault Systemes SOLIDWORKS), 2021)

### Manufacturing prep + quotes
Counts hands-on effort only.
- 2D drawings for outsourced CNC and sheet-metal parts, at NIST's 2.2-3.1 h to annotate one machined part (53-84 characteristics): low 2 parts x 2.2 h = 4.4 h; typical 4 x 2.5 h = 10 h; high 6 x 3.1 h = 18.6 h.
- CAM programming from the 3D model, at Uski et al.'s 30-60 min per part: 1 / 3 / 6 h. Their 150 min of physical tool setup per part is excluded.
- Slicing and print setup for about 10-15 printed parts: an estimate of 1 / 2 / 4 h.
- Preparing and comparing RFQs across processes: an estimate of 1 / 3 / 6 h.

Totals: about 7.5 / 18 / 34.7 h.

Waiting is NOT counted. A conventional manufacturer takes 2-3 days to return a quote (Polyverse testimonial on Protolabs), and Xometry's S-1 says 'days or weeks' to finalize price and lead time. Instant-quote platforms already cut this wait, so Morph's gain here is mainly the drawings, CAM and planning work.

Sources: [Testing the Digital Thread in Support of Model-Based Manufacturing and Inspection (Hedberg, Lubell, Fischer, Maggiano, Barnard Feeney), J. Computing and Information Science in Engineering 16(2)](https://pmc.ncbi.nlm.nih.gov/articles/PMC4904719/) (ASME / NIST, 2016); [Model-Based Definition Accelerates Product Life Cycle in Manufacturing and Inspection Phase - Experiment of Machined One-Off Production (Uski et al.), Proc. DESIGN 2022](https://doi.org/10.1017/pds.2022.66) (Cambridge University Press / Design Society, 2022); [Polyverse Solutions case study: Multi-Process Manufacturing Helps Bring New Machinery to Market Faster](https://www.protolabs.com/en-gb/resources/case-studies/polyverse-solutions/) (Protolabs (vendor case study), n.d.); [Xometry, Inc. Form S-1 Registration Statement (Business section)](https://www.sec.gov/Archives/edgar/data/1657573/000119312521181945/d120843ds1.htm) (U.S. SEC EDGAR (filed by Xometry, Inc.), 2021)

### Assembly instructions
Based on an industry estimating guide that budgets 3-5 h per page for a new-product operator or procedure manual (interviews, writing, graphics or photos, editing, index), plus about 10 h once to design a template if none exists.
- Low: 4 pages x 3 h = 12 h.
- Typical: 8 pages x 3 h = 24 h.
- High: 15 pages x 5 h + 10 h template = 85 h.

This applies a guide written for professional technical writers to an engineer-written assembly manual by analogy. It is an estimating guide, not a measured study.

Physical assembly is not included. The LeRobot SO-100 guide says about 1 h per arm, and that figure assumes someone has already written the instructions.

Sources: [A Guide to Estimating Writing Projects (V4.0)](https://cupublic.chw.org/media/BestPractices/SharedResources/Documents/EstimatingWritingProjects.pdf) (Writing Assistance, Inc. (republished by Children's Hospital of Wisconsin), 2006); [LeRobot documentation: SO-100 (assembly-time context only)](https://huggingface.co/docs/lerobot/en/so100) (Hugging Face, 2024-2025 (living doc))

### Per design change
Example change: a heavier payload forces a larger joint-2 servo. That touches links and brackets, the simulations, the power budget and possibly the PCB, plus the BOM, drawings and manual pages.
- Low 8 h: about one fingertip redesign-and-verify cycle (6-8 h, Wolniakowski et al.).
- Typical 24 h: 3 working days, the low end of Honarpardaz's 3-4 working days per manual finger-design iteration.
- High 80 h: 10 working days. Terwiesch & Loch found the hands-on activity in one engineering change order 'does not exceed 10 days'. This also matches roughly one PCB re-spin when the change reaches the board (Lifecycle Insights via Mentor: 2.9 re-spins per project at about 5.5-8.5 days each; the two versions conflict).

Elapsed-time figures are deliberately excluded: Terwiesch & Loch's 2 months to over a year per change, and Langer et al.'s self-reported mean of 60.6 working days (SD 140).

Scale context: engineering changes take about 31% of R&D capacity (Langer et al., n=27), and Soderberg (via Terwiesch & Loch) reported about 45% of engineering time going to changing parts that were 'thought to be ready'.

Sources: [Finger Design Automation for Industrial Robots: A Generic and Agile Approach (M. Honarpardaz), Dissertation No. 1917](https://www.dissertations.se/dissertation/47746412ec/) (Linkoping University, 2018); [Robotic Finger Design Workflow for Adaptable Industrial Assembly Tasks (Wolniakowski et al.), Proc. ROBOVIS 2020](https://www.scitepress.org/Papers/2020/100201/100201.pdf) (SCITEPRESS, 2020); [Managing the Process of Engineering Change Orders: The Case of the Climate Control System in Automobile Development (Terwiesch, Loch), JPIM 16(2) (INSEAD working paper)](https://sites.insead.edu/facultyresearch/research/doc.cfm?did=1023) (Journal of Product Innovation Management / INSEAD, 1999); [Modern practices in PCB design verification (citing Lifecycle Insights, Sept 2018)](https://www.designworldonline.com/modern-practices-in-pcb-design-verification/) (Design World (WTWH Media); vendor-authored (Mentor Graphics), 2019); [Exploring Differences Between Average and Critical Engineering Changes: Survey Results from Denmark (Langer, Maier, Wilberg, Munch, Lindemann), DESIGN 2012](https://strathprints.strath.ac.uk/79508/1/Langer_etal_DS70_2012_Exploring_differences_between_average_and_critical_engineering_changes.pdf) (Design Society (copy on Strathprints, University of Strathclyde), 2012)

## Caveats

TOTALS. One design revision by one experienced engineer at 8 h/day, excluding per-change work:
- Low: 6,570 min, about 110 h (about 14 workdays).
- Typical: 14,460 min, about 241 h (about 30 workdays, or 6 work-weeks).
- High: 44,740 min, about 746 h (about 4.5 months).
Each later design change adds about 1,440 min (3 days) typical, with a range of 480-4,800 min.

WHAT IS COUNTED. These are hands-on engineer-minutes only. The baseline leaves out:
- Waiting and calendar time: RFQ turnaround of 2-3 days to weeks, PCB fabrication of at least 24 h plus shipping, Aberdeen's 5.4 days to build a prototype, and change orders that take months elapsed.
- Physical printing, machining and assembly.
- Review meetings.
On calendar time the gap would look much larger. Predictable Designs gives 2-5 weeks each for schematic and layout and 1-6 months for firmware, and the Aberdeen survey for Siemens averages about 3 months from schematic to release. Do not put a calendar figure next to Morph's ~1 minute without labelling it.

WHERE THE DATA IS THIN. Several numbers are my estimates, informed by the cited sources rather than taken from them:
- Arm CAD: only gripper and finger figures exist.
- MuJoCo/MJCF setup: surveys show it is manual and hard but give no hours.
- Firmware, spec, harness, print slicing, and RFQ prep effort.
The 13% "developing system specs" share is not one of the pre-verified figures. I read it myself from the same p.38 chart of the verified 2019 Embedded Markets Study; the 2019 bars add to 99%.

VENDOR SOURCES. Many figures come from vendor-run or vendor-sponsored surveys and case studies: Fictiv, Accuris, CADENAS, Tech-Clarity, CalcuQuote, trinckle, Protolabs and Mentor/Lifecycle Insights. Cite them as such. The two conflicting Lifecycle Insights versions (8.5 days vs about 5.5 days per re-spin) are why only ranges are used.

FIGURES NOT TO STRETCH. Do not apply these directly to this robot:
- Sandia's 317-1,798 h per analysis.
- DAA's 63 h of FEA preprocessing.
- NIST's 60.3 h cycle, which is mostly the supplier re-modelling parts from drawings.
- The Embedded study's 12.2-month projects.
- Langer's 60.6 working days.
Rejected figures, not used: Robinson 2010 / IEEE "39 days", and roboticscenter.ai's SO-101 times.

FRAMING FOR THE PITCH. Morph's output is a first draft that an engineer still has to review. For an honest before/after card, add review time to the "after" side, for example "~1 min to generate + a few hours of engineer review vs ~6 work-weeks (typical) by hand". No verified IDC or McKinsey "time spent searching" figure was found, so don't use the commonly repeated IDC search-time statistic.

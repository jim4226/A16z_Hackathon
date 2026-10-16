"""Common words: the English words of Morph's own documentation and common robotics, electronics, firmware and machine
learning words and abbreviations, 3 to 10 lower-case letters each (2,526 of them). The key filter (security._phrase,
security._words) uses them to tell a name from a key: a key's random groups are almost never one of them (a random
group of 4 lower-case letters and digits is one about 2.6 times in 10,000, of 5 about 0.8 times in 100,000), names are
made of them (mt_SG90_horn_trim_offset, maxPWMFreq)."""

WORDS = frozenset("""
abort about above absolute acc accel accepts access account accuracy accurate ack across act action activation
active activity actuator actuators adam adapter adc add added adding additions address addresses adds adjust
affected after afterwards again against agent agents ago agree aimed air alarm align all allow allowed allowing
allows alone along alpha already also always amp amplitude amps analog analysis anchor anchored and angle angles
angular animation ankle another answer answered answering answers antenna anthropic any anyone anything anyway
anywhere apart aperture api app appear appears appended applicable applied applies apply approach approval approvals
approve approved approvers approves approving arc archive archives arduino are area areas aren argument arguments
arm armature arms around array arrive arrived arrives arriving arrow aside ask asked asking asks assembly assistant
assumed assumes assumption attach attention audio audit augment auth author auto aux average averages avg away axes
axis axle back backbone background backlash backoff backup bad balance ball band bandwidth bar bare barrel bars base
baseline bash bat batch batt battery baud bay bbox beam bearing bearings because become becomes bed been before
behind being belong below belt bench bend beside best beta between beyond bias big bigger bin binary bins bit bits
black blade blend block blocked blocks blue board boards bodies body bolt bolts bom boom boost boot bootloader bot
both bottom bought bound boundary bounding bounds box boxes bracket brackets brain brake branch branches breaks
bridge brief bright brightness bring brings broken broker brown browser browsers brush brushed brushless buck budget
buf buffer bug build builder building builds built bulk bump bundle bus business busy but button buttons cable
cables cache cached cad cal calendar calib calibrate call called calls cam came camera can cancel cancelled
candidate cap capability capacitor capacity capital capitals capped caps capsule caption capture car carbon card
cards carried carrier carries carry carrying cart carton cascadio case cast catalog catch category caught cause cell
cells center centre certify cfg chain chance change changed changes changing channel channels characters charge
charger chart chassis chat cheaper check checked checklist checkpoint checks chip chips choice choose chose chosen
chunks circle circuits cited claim claims clamp clamped clamps clash class classes classifier claude clean cleanup
clear clearance cleared clears click clicker client climbs clip clock clockwise clone close closed closer closes
closest closing cloud cls cluster cmd coarse code codeload codes coil cold collar collision color colour column come
comes command commands comment comments commercial commit committed common comms comp compared comparing comparison
compass compiled complete completed component components compute computed computer computers computing conf
confidence config confirm conflict conn connected connection connector connectors connects console contact contacts
contain contains content context continue continued continues continuing contour contract contracts control
controller controls conv cookie cookies coord coords copied copies copper copy core cores corner cost costs could
couldn count counted counter counting counts coupler cover covered covers cpu cracking cracks crane crank crash
crashed create created creates creating creator credential critical crop cross ctl ctrl cube cubed cur curated curr
current curve custom cut cuts cutting cycle cylinder dac damage damaged damper damping dark darwin data dataset
datasheets day days dead deadband deadline debug debugging dec decay decide decided decides decision decisions
declared declares declined decoder deep deepen deeper default defaults definition deflection deg degrees delay
delete deleted deletes deleting deletion delta demo density depend dependency depending depends depth derived
descent describe describes design designing designs desk desktop det detailed details detect detector dev developers
developing device dial dialog dialogs diameter did didn diff differ difference different differs diffusion digit
digital digits dim dimensions diode dir direct direction directly disable discard discipline disk dismiss display
dist distance distances distinct divider dock docs doctor document documented documents does doesn doing don done
door dot doubled doubles dovetail down download downloads draft drafted drafting drafts drag dragged drain draw
drawings drawn draws drift drive driver drives driving drop dropout dropped drops dry dst dual during duty dynamic
dynamixel each earlier east easy echoes edge edges edit editable edited editing editor editors edits effect
effective effector effort egg eggs eight either elbow element else elsewhere embed embedding emergency empty enable
enabled enc encoder end ended endpoint ends energy engine engineer engineers enough entirely entities entries entry
env envelope epoch epochs equal erased err error errors estimate estimated estimates estimating estop eval even
event events ever every everyone everything everywhere exact exactly example examples except exception excluded
excluding executes exist existing exists exit exits exp expanded expected expert expired expires expiry explains
explicit export exported exports extends extension extra extract extras extruder eye fab fabricate fabricated face
faces factor fail failed failing fails failure failures fall fallback fallbacks falls false families family fan far
fast fastener faster fault feasible feature features fee feed feedback feeding feeds feet femur fet fetch few fewer
ffmpeg field fields fifo figure figures file files fill filled fills filter final finalizers find finding findings
finds fine finetune finger fingers fingertip finish finished finishes finite fire firmware first fit fits five fix
fixed fixes fixture fixtures flag flagged flags flagship flash flat flex flip float flood floor flow flushed fly
foam focus fold folder folders folds follow follower follows font fonts food foot for force forces fork form format
formerly forms forward found four fps fragile frame frames free freed frees freq frequency fresh friction from front
frozen fsynced full further fuse fusion gain gains gap gaps gate gated gauge gear gearbox gears gen generalist
generated generates generator generic gentler geometry gerbers get gets git github give given gives glass global
glued goal goals goes going golden gone good gpio gps gpu grab grad gradient graph graphics grasp gravity gray great
green grey grid grip gripper grips ground grounding group grouped groups grown guard guesses guide guided gyro
hackathon had half hall hand handle handler handling handoff handoffs hands happens hard hardened hardening hardware
harmful harness has hash hashed hashes have haven head header headers headline heads heartbeat heat heater heavier
heavy height held help helper helpers helps her here hex hidden hide hides high higher him hinge hip his history hit
hits hold holding holds holes home homepage honest honesty hook hooks hop horn host hosted hostile hosts hot hour
hours how however http https hub human hundreds idle ids idx ignored ignores image images img imgs implement
implements import imported importing imports imu inbox include included includes including index industrial infer
inference informed init inner input inputs inside inspector inst install installed installer installing installs
instance instead int interface internet interval intervals into inverse invite invites isn isolates issue issues
item items iteration its itself jacks jam jams jaw jaws jnt job jobs jog join joined joins joint joints journal
journals jumbo just keep keeps keewano kept kernel key keys keyword keywords kick kill killed kind kit kitchen knee
know known knows lab label labelled labels lamp land lands laptop large larger largest laser last latch late latent
later latest launch launcher launchers launches layer layers layout lead leader learn least leave leaves led ledger
left leg len length lens less let lets letter letters level lever library licence licences license licensed lidar
life lifecycle lift lifts light lighter lights like likely lim limit limited limits line linear lines link links
lint lipo list listed listens lists little live lnk load loaded loading loads local lock locked log logged logic
logs long longer longest look looking looks lookup lookups loop loopback lora loses loss lost low lower luggage
machine machined made mag main make makes making malicious man manager manifest manifests manual manually many map
maps margin margins mark markdown marked marker marks mask mass masses master match matches material materials
matrix matters max may maze mcp mean means meanwhile measure measured measures measuring mechanical mechanism media
medium meetings member members memory menu merge merged mesh meshes message messages meta metadata metal meter
method metric metrics micro mid middle might million min mini minus minute minutes mislead miss missed misses
missing mistyped mix mock mode model models module modules moment momentum money monitor month months more morph
most mot motion motor motors mount mounted mounts mouse mouser move moved moves moving msg mtime much mujoco
multicast must myself name named names naming nano narrow narrower native near nearby nearest nearly neck need
needed needs neither nested net network never new newer newest next nice night nobody node noise noisy nominal none
nor norm normal normally north not note noted notes nothing notices now nowhere nozzle npm null num number numbered
numbers numpy nut nuts obj object objects off offered offers official offline offset offsets often old older oldest
once one ones online only open opened opening opens opinion opt optical optim option optional options orange order
ordered ordering orders orient origin original other others otherwise our out outcome outcomes outdated outer output
outputs outside over overrides own owner owners ownership owns pack package packages pad pads page pages paid pan
panel parallel param parameter parameters params parent parity parse part partial partner parts pass passed passes
password past paste pasted patch path paths pattern patterns pause paused pauses payload payment pays pcb peak
pending people per percentile person personal phase phone photo photos phrase physical physics pick picked picking
picks pid pilot pin pink pinned pins pip pipe pipeline pitch pivot place placed placement places plain plan planned
planner planning plans plant planted plants plate plates platform platforms play plays playwright please plus point
pointer points pole policy polygon pool port ports pos pose posed position post pour pouring power practices
predicted prefix preflight prep presence present preset presets press presses pressure preview previews previous
price prices print printable printed printer printing prints prior priority priors privacy private privately prob
probe problem problems process processes produces product production profiles program programs progress project
projects prompt prop property proposal propose proposes protects protocol protocols prototype prove provider
providers proxy prune public publish published pull pulley pulse pump purchased pure purple purpose push put puts
pwm pwr pyproject pytest python quad quant quantile quantity quantized queries query question questions queue queued
queues quick quiet quote quoted quotes quoting racks radio radius rail rain raise raised raises ramp ran random
randomized range ranges rank rate rated rather ratio raw reach reached reaches read readable reading reads ready
real rear reason reasons reboot rebuild rebuilding rebuilds rebuilt recall receives recognised recompute recomputes
record recorded recording records red redirect redirects redraw redraws reduced reed ref reference refreshes refusal
refuse refused refuses regenerate register registered registers reject rejected related relative relay relayed
release released releases relies reload reloaded reloads remains remembered remote remove removed removes removing
renamed rendered renders reopened reopens reordered repeat repeats replace replaced replaces replacing replay
replayed replays reply repo report reported reporting reports repository req request requests required reruns res
research reserved reset resets resolve resolved resolves resp rest restart restarted restarting restarts result
results resumable resume resumed resumes retention retried retries retry return returned returns reuse reused reuses
rev reverse review reviewer reviewers reviews revision revisions revoke revoking reward rewrite ride rides rig right
rights ring rise road robot robotics robots robustness rock rod role roles roll room rooms rot rotation rotor
roughly round rounded rounds route routed router routes routing row rows rpm rule rules run runner running runs
runtime safe safety said same sample samples sampling sandbox save saved saves saving say says scalars scale scaled
scales scaling scan scanned scans scenario scenarios scene schedule scheduler schema schemas schematic scipy scope
scoped score scratch screen screw screws script scripted scripts scrolling scrolls sea seal search searched searches
searching second seconds secret section sections security see seed seeded sees seg segment select selected selection
sell sells send sending sends sense sensible sensing sensor sensors sent sentence separate separates serial series
serve served server servers serves service services servo servos session sessions set sets setting settings settle
settles setup several sha shaft shake shallower shape shapely shapes shard share shared shares she sheet shell shift
shim ship shipped shipping ships short shortened shorter shortlist should shoulder show showed showing shown shows
shuffles shut shutdown side sideways sig sight sigma sign signal signatures signed signing signs silicon sim simple
simulate simulated simulates simulation simulator since single sit site sites sits six size sized sizes sizing skin
skip skipped skips slave sleep slicing slide slider sliding slightly slip slot slots slow slower small smooth snap
socket soft softer softmax software solenoid solids solver some someone something sort source sources sourcing south
space spare spawn spawned spd spec specialist specs speed spend spending spends spent sphere spi split sponsor spot
spots spring square src srv stage stages stale stall stand standard stands star start started starting starts
startup state statement static status stay stayed staying stays std stdout steer step stepper steps stiff still
stock stop stopped stops storage store stored storing straight strain strawberry stream streams strength stress
stretch strict stride string strip stroke strong stronger structured stub student studied studies study style sub
subq subscribes subset succeed succeeds success successes successful such suggested suggests sum summary sun
supplier suppliers supply support supported sure surface survey surveys survives swap sweep sweeps swing switch
switched switches switching sync synced system systems tab table tables tabs tag tagged tagging tags tail take taken
takes talk talking tall tampering tape tare target targets task tasks teacher team teammate teammates teams
technical telemetry tell tells temp template temporary ten tension tensor terminal terms test tested testing tests
text than that the their them themselves then there thermal these they thick thin thing things thinking this those
thr thread threads three throttle throttled through thumb tick tied tilt time timed timeline timeout timer times
timestep timing timings tiny tip title titles tmp today toe together token tokens told tolerance tolerances too took
tool toolbar toolbox tooldraft tools top torque torques total touch touched touches touching tour trace traceback
track trail train trainer training trajectory transport trash travel treat treats tree triage trials triangle
triangles tried tries trim trimesh trinckle trip true trust truth try tune tuned tuning tunnel turn turned turning
turns twice twist two type typed types typical typing uart unchanged under unet unexpected unit units unknown unless
unreleased unstable until untouched unzip update updated updates updating upgrade upload uploaded uploading uploads
upper url usable usage usb use used user uses using usually val valid validation value values valve vanished
variable variant varies vary vector vel velocity vendor vendored vent venv verdict verified version versioned
versions very via vid video view viewer viewers views violations vision visual volt voltage wait waited waiting
waits wake walk walking walkway wall walls want warm warning warnings warns was watch water waters watertight way
ways weaker wear web website websites week weeks weight weights well went were west wet what whatever wheel wheels
when whenever where wherever whether which while white who whoever whole whom whose why wide wider width wild will
win wind window windows wing wins wire wiring with within without won wood word wording words work workbench
workdays worker workers workflow working works workspace world worlds worst worth would wraps wrist write writes
writing written wrong wrote xacro yaw year yellow yes yet you young your yours yourself zero zip zipped zips zone
""".split())

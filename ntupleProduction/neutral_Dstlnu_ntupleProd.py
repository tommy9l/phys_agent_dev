import basf2 as b2
import modularAnalysis as ma
import variables.collections as vc 
from variables import variables as vm
import variables.utils as vu 
from variables.MCGenTopo import mc_gen_topo
import sys 
import vertex
# Check the skim output
from skim.WGs import fei, semileptonic

if len(sys.argv) != 2:
    sys.exit("Usage: neutral_Dstellnu_ntupleProd.py <e/mu>")

mode = sys.argv[1]
if mode not in ["e", "mu"]:
    sys.exit(f"Expecting either e or mu. Saw {mode} Try again")

"""
Let's try to write this python file in a format 
that can be used by agents or batch systems. 

So components of the code should be modular to assist with this. 
"""

# run 1
b2.conditions.prepend_globaltag(ma.getAnalysisGlobaltag()) #needed for FEI 
b2.conditions.prepend_globaltag("release-08-00-09")
b2.conditions.prepend_globaltag('neutrals_2024')


# dummy name for input, can overwrite in basf2 using -i option on command line
rootInputName = "B0_Dstlnu_mdst.root" # contains both e and mu

# dummy name for output, can overwrite in basf2 using -o option on command line
rootOutputName = f"B0_Dst{mode}nu_ntuple.root"

# create path
path = b2.create_path()

# read in input
HOME=".."
inputPath = f"{HOME}/data/"
ma.inputMdstList(filelist = [f"{inputPath}/{rootInputName}"], path = path)

# start with final state particles 
# TODO: we need to have some minor cuts for purity
# TODO: Look at b2help-recommendation (https://belle2.pages.desy.de/performance/recommendations/)

# for when we want to see how our selection does w/ existing skims
skim_list = {
    #"feiSL": fei.feiSL(udstOutput = False), 
    #"feiHadronicB0": fei.feiHadronicB0(udstOutput = False), 
    #"feiSLB0_RDstar": fei.feiSLB0_RDstar(udstOutput = False),
    #"feiSLB0": fei.feiSLB0(udstOutput = False),
    #"feiHadronic_DstEllNu": fei.feiHadronic_DstEllNu,
    "SLUntagged": semileptonic.SLUntagged(udstOutput = False), 
}
for key, value in skim_list.items():
    value(path=path)
    vm.addAlias(f"skim_{key}", value.flag)



# b2help track recommendations 
list_track_cuts = [
    "thetaInCDCAcceptance",
    "dr < 1",
    "abs(dz) < 3"
]
track_cuts = " and ".join(list_track_cuts)


# ROE mask for continuum suppression -- b2help
# these tracks come from IP so we should be careful 
track_selection = " and ".join([
    "[dr < 2]",
    "[abs(dz) < 4]",
    "[pt > 0.2]",
    "[thetaInCDCAcceptance==1]"
])

track_displaced_selection = " and ".join([
    "[thetaInCDCAcceptance==1]"
])

slow_pion_cut = " and ".join([
    "[dr < 2]",
    "[abs(dz) < 4]",
    "[thetaInCDCAcceptance==1]"
])

photon_cuts = " and ".join([
    "[abs(clusterTiming) < 200]",
    #"[E > 0.1]", #TODO: do we want this? 
    "[thetaInCDCAcceptance==1]"
])
# so I am reconstructing B0 -> D* e nu 
# maybe easier to keep e and µ channels separate
# where D* -> D0bar (-> K pi) pi
# start with final state particles and build up from there

# loose PID cuts
vm.addAlias("kaonBinaryPID", "binaryPID(321, 211)")
vm.addAlias("pionBinaryPID", "binaryPID(211, 321)")
PID_Cuts = {
    "pi+":  "pionBinaryPID > 0.1",
    "K+":  "kaonBinaryPID > 0.1"
}

# signal side final state particles
ma.fillParticleList("e+:sig", f"{track_cuts}", path=path)
ma.fillParticleList("mu+:sig", f"{track_cuts}", path=path)
ma.fillParticleList("K+:D", f"{track_cuts} and {PID_Cuts['K+']}", path=path)
ma.fillParticleList("pi+:D", f"{track_cuts} and {PID_Cuts['pi+']}", path=path) 
ma.fillParticleList("pi+:Dstr", f"{slow_pion_cut} and {PID_Cuts['pi+']}", path=path) # slow pion
ma.fillParticleList("pi+:KS", f"{track_displaced_selection}", path=path) # for creating k shorts
ma.fillParticleList("gamma:pi0",
                    "[[clusterReg == 1 and E > 0.08] or [clusterReg == 2 and E > 0.03] "
                    "or [clusterReg == 3 and E > 0.06]] and abs(clusterTiming) < 200",
                    path=path)
#ma.reconstructDecay("pi0:std -> gamma:pi0 gamma:pi0", "0.10 < InvM < 0.16", path=path)
ma.reconstructDecay("pi0:D -> gamma:pi0 gamma:pi0", "0.12 < InvM < 0.15", path=path)
ma.reconstructDecay("pi0:Dstr -> gamma:pi0 gamma:pi0", "0.11 < InvM < 0.15", path=path)
ma.copyLists('pi+:std', ['pi+:D', 'pi+:Dstr'], path=path)
ma.copyLists('pi0:std', ['pi0:D', 'pi0:Dstr'], path=path)
# paper mentions ZernikeMVA but this is unreliable if we cannot guarantee track/cluster isolation 

ma.reconstructDecay("K_S0:std -> pi+:KS pi-:KS", "0.40 < M < 0.60", path=path)
#TODO: do slow pion efficiency corrections at some point --> maybe when we use Run Dependent
#TODO: do photon energy corrections as well 
#TODO: K shorts will also need a correction

# Do I need to do photon corrections or is this built in? 

# We want to apply cut on event shape. For this, we are creating events shape object
# First, create a list of good tracks (using the pion mass hypothesis)
# and good gammas with very minimal cuts
ma.fillParticleList(decayString='pi+:goodtracks',
                    cut=f'{track_selection}',
                    path=path)
ma.fillParticleList(decayString='gamma:goodclusters',
                    cut=f'{photon_cuts}',
                    path=path)

# Second, create event shape
ma.buildEventShape(inputListNames=['pi+:goodtracks', 'gamma:goodclusters'],
                   allMoments=True,
                   foxWolfram=True,
                   harmonicMoments=True,
                   cleoCones=True,
                   thrust=True,
                   collisionAxis=True,
                   jets=True,
                   sphericity=True,
                   checkForDuplicates=False,
                   path=path)
ma.buildEventKinematics(inputListNames=['pi+:goodtracks', 'gamma:goodclusters'], path=path)

# optional: cut on event levels if desired

# reconstruct signal neutral B
D_mass = "1.75 < M < 2.0" # wide enough for both D0 and D±
D_modes = {  # dmID: decay string (K-like first, pi0 last)
    "D0": ["K-:D pi+:D", "K-:D pi+:D pi0:D", "K-:D pi+:D pi-:D pi+:D", "K-:D K+:D", "pi-:D pi+:D",
                "K_S0:std pi0:D", "K_S0:std pi+:D pi-:D",  "K_S0:std pi+:D pi-:D pi0:D"],
    "D-":      ["K+:D pi-:D pi-:D", "K_S0:std pi-:D",
                "K_S0:std pi-:D pi0:D", "K_S0:std pi-:D pi-:D pi+:D"],
}
for D, modes in D_modes.items():
    for i, daus in enumerate(modes):
        ma.reconstructDecay(f"{D}:m{i} -> {daus}", D_mass, dmID=i, path=path)
    ma.copyLists(f"{D}:Dst", [f"{D}:m{i}" for i in range(len(modes))], path=path)

Dstr_cut = "massDifference(0) < 0.2"
ma.reconstructDecay("D*-:D0pi -> anti-D0:Dst pi-:Dstr", Dstr_cut, dmID=0, path=path)
ma.reconstructDecay("D*-:Dpi0 -> D-:Dst pi0:Dstr",      Dstr_cut, dmID=1, path=path)


#now combine the two modes together
ma.copyLists('D*-:sig', ['D*-:D0pi', 'D*-:Dpi0'], path=path)

b_cut = "Mbc > 5.2 and abs(deltaE) < 0.3"
if mode == "e":
    ma.reconstructDecay(
        "B0:sig -> D*-:sig e+:sig ?nu", b_cut, path=path)  
if mode == "mu":
    ma.reconstructDecay(
        "B0:sig -> D*-:sig mu+:sig ?nu", b_cut, path=path)  

# perform vertex for B0 reconstruction
# standard vertex algorithm for Belle II is the treeFit algorithm
# TODO: We actually need to do this twice. Need to update this later... 
# TODO: In the fastSim version, I don't think any vertex fitting is done... plz check

ma.variablesToExtraInfo("B0:sig", {"daughter(0, massDifference(0))": "preFit_deltaM",
                                   "daughter(0, daughter(0, dM))": "preFit_D_dM"}, path=path)

vertex.treeFit("B0:sig", conf_level = 0.0, 
                updateAllDaughters=True , path=path)
 

ma.rankByHighest("B0:sig", "chiProb", outputVariable="B_rank_chiProb", path=path)


#ma.applyCuts('B+:feiHadronic', 'Mbc > 5.2 and -0.25 < deltaE < 0.3 and cosTBTO(fei_mask) < 0.99', path=path) #broaden for more studies


# so we have truth information
ma.matchMCTruth(list_name = "B0:sig", path = path)


ma.buildRestOfEvent('B0:sig', inputParticlelists = ['K+:D','e+:sig','mu+:sig', 'pi+:D'] ,path=path)

ecl_selection = "[[[clusterReg==1] and [E>0.080]] or [[clusterReg==2] and [E > 0.03]] or [[clusterReg==3] and [E > 0.06]]] "
ecl_selection += "and [clusterNHits > 1.5] and [0.2967 < clusterTheta < 2.6180] and [abs(clusterTiming) < 200]"
roe_mask = (track_selection, ecl_selection)

ma.appendROEMask('B0:sig', 'roe_mask', *roe_mask, path=path)


# working for rest of event: 
roe_path = b2.create_path()
# Create an empty path to interrupt the execution in case of a different particle list
deadEndPath = b2.create_path()

# Execute the filter module:
ma.signalSideParticleFilter('B0:sig', '', roe_path, deadEndPath)

good_roe_photons = "isInRestOfEvent == 1 and E > 0.1" #  and beamBackgroundSuppression > 0.1 and fakePhotonSuppression > 0.1
bad_roe_photons = "isInRestOfEvent == 1 and E > 0.05"
# Filling example ROE photons:
ma.fillParticleList('gamma:bad', f'{bad_roe_photons}', path = roe_path)
ma.fillParticleList('gamma:good', f'{good_roe_photons}', path = roe_path)
# Discard or keep ROE photons, other types of particles are not modified:
ma.discardFromROEMasks('gamma:bad',['roe_mask'],'', path = roe_path)
ma.keepInROEMasks('gamma:good',['roe_mask'],'',path = roe_path)
# Fill ROE pion particle list:
ma.fillParticleList('pi+:roe', 'isInRestOfEvent == 1', path = roe_path)
# Reconstruct a K_S0 candidate using ROE pions:
ma.reconstructDecay('K_S0:roe -> pi+:roe pi-:roe', '0.45 < M < 0.55', path = roe_path)
# Perform vertex fitting:
vertex.treeFit('K_S0:roe',conf_level = 0.0, path=roe_path)
# Insert a K_S0 candidate into the ROE mask:
ma.optimizeROEWithV0('K_S0:roe',['roe_mask'],'', path=roe_path)
# Execute loop for each ROE:
path.for_each('RestOfEvent', 'RestOfEvents', path = roe_path)
# reconstruct rest of event (tag side)
# TODO: inputParticlelists should be corrected/calibrated FSPs. Update in future 

#ma.buildContinuumSuppression('B0:sig', 'fei_mask', path=path)
# Required selections on tag candidates
#ma.applyCuts('B+:feiHadronic', 'Mbc > 5.27 and -0.15 < deltaE < 0.1 and cosTBTO(fei_mask) < 0.9', path=path) #default 
#ma.applyCuts('B+:feiHadronic', 'Mbc > 5.2 and -0.25 < deltaE < 0.3 and cosTBTO(fei_mask) < 0.99', path=path) #broaden for more studies
# Best candidate selection
#ma.rankByHighest('B+:feiHadronic', 'extraInfo(SignalProbability)', outputVariable='sigProb_rank', path=path)
#ma.cutAndCopyList('B+:BestCandidate', 'B+:feiHadronic', 'sigProb_rank == 1', path=path)


# reconstruct upsilon(4S)
#ma.reconstructDecay(  # [S70]
#    "Upsilon(4S):opposite_cp -> B0:feiHadronic anti-B0:signal", cut="", path=path
#)
#ma.reconstructDecay(
#    decayString="Upsilon(4S):same_cp -> B0:feiHadronic B0:signal",
#    cut="",
#    path=path,
#)
# Combine the two Upsilon(4S) lists to one. Note: Duplicates are removed.
#ma.copyLists(
#    outputListName="Upsilon(4S)",
#    inputListNames=["Upsilon(4S):opposite_cp", "Upsilon(4S):same_cp"],
#    path=path,
#)  # [E70]

# use collections to save variables
# variations on variables
cms_kinematics = vu.create_aliases(vc.kinematics, "useCMSFrame({variable})", "CMS")
pidvars = vc.pid #[f"{p}NN" for p in vc.pid[0:6]] + vc.pid 
#PID variables not available in release/08-02, might need light release
skim_vars = [f"skim_{key}" for key in skim_list]

event_vars = ['nTracks','L1Trigger','HighLevelTrigger','nExtraCDCHits','nExtraCDCSegments', "nECLOutOfTimeCrystals",
              'beamE', 'beamPx', 'beamPy', 'beamPz', 'Ecms'] + vc.event_kinematics + vc.event_shape + vc.mc_event_kinematics

#TODO: Do we need MC Kinematics? 
# collecting variables
ks_vars = [ "chiProb", "distance", "significanceOfDistance",
           "cosAngleBetweenMomentumAndVertexVector"]

fsp_vars = vc.mc_variables + vc.mc_truth + vc.kinematics + cms_kinematics + \
            pidvars + vc.track + vc.track_hits 
d_vars = vc.mc_truth + vc.kinematics + cms_kinematics + vc.inv_mass  + \
            vc.vertex + vc.mc_vertex + ['foxWolframR2', 'cosAngleBetweenMomentumAndVertexVector']
b_vars = vc.mc_truth + vc.vertex + vc.kinematics + vc.inv_mass + \
    vc.deltae_mbc  + \
    vu.create_aliases_for_selected(
        fsp_vars, f"B0 -> [D*- -> [anti-D0 -> ^K+ ^pi- ^pi- ^pi+] ^pi-] {mode}+",
        prefix=["D_d0", "D_d1", "D_d2", "D_d3", "Dstr_soft"],
    ) + \
    vu.create_aliases_for_selected(
        ks_vars, f"B0 -> [D*- -> [anti-D0 -> ^K_S0 pi+ pi-] pi-] {mode}+", prefix="D_d0") + \
    vu.create_aliases_for_selected(list_of_variables=fsp_vars, 
                                   decay_string=f'B0 -> D*- ^{mode}+',
                                   prefix = f"{mode}sig") + \
    vu.create_aliases_for_selected(d_vars, f"B0 -> ^D*- {mode}+", prefix="Dstr") + \
    vu.create_aliases_for_selected(d_vars, f"B0 -> [D*- -> ^anti-D0 pi-] {mode}+", prefix="D") 

b_vars += vu.create_aliases(["decayModeID"], "daughter(0, extraInfo({variable}))", "Dstr")
b_vars += vu.create_aliases(["decayModeID"], "daughter(0, daughter(0, extraInfo({variable})))", "D")
b_vars += ["chiProb", "extraInfo(B_rank_chiProb)"]   # drop "chiProb" if vc.vertex already has it
# add some rest of event information
roe_kinematics = ["roeE(roe_mask)", "roeM(roe_mask)", "roeP(roe_mask)", "roeMbc(roe_mask)", "roeDeltae(roe_mask)", "roeEextra(roe_mask)"]
roe_multiplicities = [
    "nROE_Charged(roe_mask)",
    "nROE_Photons(roe_mask)",
    "nROE_NeutralHadrons(roe_mask)",
]
b_vars += roe_kinematics + roe_multiplicities + \
            event_vars + skim_vars + vc.event_level_tracking  + \
            ["extraInfo(preFit_deltaM)", "extraInfo(preFit_D_dM)"]

photon_vars = vc.cluster
pi0_vars = vc.mc_truth + vc.kinematics + vc.inv_mass + \
    vu.create_aliases_for_selected(list_of_variables=photon_vars, 
                                    decay_string=f'pi0 -> ^gamma ^gamma')



# sanity check here: 
vm.printAliases()

outputPath = f"{HOME}/data"
# VariablesToNtuple
ma.variablesToNtuple(
    "B0:sig",
    variables=b_vars ,
    filename=f"{outputPath}/{rootOutputName}",
    treename="ntuple",
    path=path,
)


b2.process(path)
print(b2.statistics)
print("completed!")
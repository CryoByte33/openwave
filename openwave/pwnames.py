"""OpenWave's PipeWire node-name vocabulary — every ``openwave_*`` name in one
place.

Which routing element a node represents is domain knowledge, so the names live
here; the PipeWire adapter that creates and destroys them is name-agnostic. See
CONTEXT.md for src-sink, mix bus, cell, capture device, and loopback.
"""

PREFIX = "openwave_"

# Mix buses (null sinks) the matrix routes sources into. mix_id -> node name.
MIX_SINKS = {
    "personal": "openwave_personal_mix",
    "chat":     "openwave_chat_mix",
    "record":   "openwave_record_mix",
}
PERSONAL_MIX_SINK = MIX_SINKS["personal"]

# Human-readable description shown for each mix-bus sink. mix_id -> label.
MIX_SINK_DESCRIPTIONS = {
    "personal": "OpenWave Personal Mix",
    "chat":     "OpenWave Chat Mix",
    "record":   "OpenWave Record Mix",
}

# Chat/Record exposed to apps as named capture devices.
# mix_id -> (sink_description, capture_source_node, capture_source_description)
MIX_DEVICES = {
    "chat":   (MIX_SINK_DESCRIPTIONS["chat"],   "openwave_chat",   "OpenWave Chat"),
    "record": (MIX_SINK_DESCRIPTIONS["record"], "openwave_record", "OpenWave Record"),
}
# The capture-device source nodes (what other apps select as a mic).
CAPTURE_SOURCE_NAMES = tuple(src for _, src, _ in MIX_DEVICES.values())

# Always-on loopback that feeds the Personal mix to the headphones.
HP_LOOPBACK_NODE = "openwave_loop_personal_to_hp"

# Substring matching the Wave XLR's ALSA nodes — loose on purpose so it covers
# both the MK1 ("Elgato_Systems_…") and the MK2 ("Elgato_Elgato_…_MK.2") naming.
WAVE_XLR_MATCH = "Wave_XLR"

# pkill pattern that reaps every loopback we spawn (cells, mic, HP, capture cap)
# — they all carry an ``openwave_loop_*`` node on their command line.
LOOPBACK_SWEEP = "pw-loopback.*openwave_loop_"


def src_sink(source_id):
    """A source's private null sink; its app streams are moved onto this."""
    return f"openwave_src_{source_id}"


def cell_loop(source_id, mix_id):
    """Playback node of the loopback carrying a source into a mix bus."""
    return f"openwave_loop_{source_id}_to_{mix_id}"


def cap_node(loop_node):
    """Capture-side node paired with a loopback's playback node."""
    return f"{loop_node}_cap"


def capture_src_cap(mix_id):
    """Capture node of a Chat/Record capture-device loopback."""
    return f"openwave_loop_{mix_id}_src_cap"


def is_ours(node_name):
    """True for any node OpenWave created — used to skip our own streams."""
    return node_name.startswith(PREFIX)

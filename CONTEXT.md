# OpenWave

A Linux control app for the Elgato Wave XLR microphone: USB hardware control plus a GoXLR/Wave-Link-style submixer built on PipeWire.

## Language

**Source**:
An audio origin in the matrix — the mic, the System catch-all, or a user-added app. Each app source's streams are moved onto its own sink so the source has one stable output.
_Avoid_: input, channel, app

**System source**:
The catch-all source; every app stream not matched to a user-defined source is routed here.
_Avoid_: default, misc, other

**Mix bus**:
One of the three destinations a source can be routed into — Personal, Chat, or Record. Personal feeds the headphones; Chat and Record are capture devices.
_Avoid_: output, channel, group

**Cell**:
The matrix intersection of a source and a mix bus — a per-(source, mix) send level plus mute. Subtractive: lowering a cell removes that source from that mix.
_Avoid_: send, knob, slot

**Master fader**:
A source's GoXLR-style channel fader; the effective send is cell × master. The mic's master is a software level — its hardware gain stays in the sidebar.
_Avoid_: channel volume, gain (gain is the hardware input level only)

**Capture device**:
A mix bus (Chat or Record) exposed to other apps as a selectable, named PipeWire source.
_Avoid_: virtual mic, monitor

**Loopback**:
A `pw-loopback` subprocess carrying audio along one edge of the routing graph — a cell, mic→mix, Personal→headphones, or a capture device.
_Avoid_: pipe, bridge, tap

**src-sink**:
A source's private null sink. The source's app streams are moved onto it (PipeWire `target.object`) so its monitor is the source's single stable output.
_Avoid_: null sink (alone — there are several kinds)

**Node name**:
The `openwave_*` PipeWire identifier of a routing element (src-sink, mix bus, loopback, capture device). The naming scheme is domain knowledge and lives in one place; the numeric handle is the **node id**, not the node name.
_Avoid_: node id (that is the numeric handle)

**Keepalive**:
The background daemon that holds a `pw-cat` capture open on the Wave XLR and recycles it when the device wedges (alive but passing no data).
_Avoid_: watchdog (alone), audio service

**Device backend**:
The USB-control implementation for one hardware revision — `WaveXLR` (MK1) or `WaveXLRMk2` (MK2) — behind a shared interface.
_Avoid_: driver, device class

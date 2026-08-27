import QtQuick
import Quickshell.Io
import qs.Ui

// Arm/disarm the "hey jarvis" wake-word listener.
//
// The listener is a systemd user unit that holds the mic open and runs
// openWakeWord continuously (~3% of one core), so it is off by default and
// this widget is the switch. Left click toggles it, right click restarts it.
// Pipeline state comes from a small file the daemon writes, which keeps this
// widget from having to talk to the process at all.
BarWidget {
  id: root
  moduleName: "dorian.voice"

  readonly property string unit: setting("unit", "jarvis")
  readonly property int pollSeconds: Math.max(1, setting("pollSeconds", 1))
  readonly property bool notify: setting("notify", true)

  // serviceState: "active" | "inactive" | "unknown"
  // pipeline: "idle" | "listening" | "thinking" | "speaking" | "off"
  property string serviceState: "unknown"
  property string pipeline: "off"
  property bool busy: false

  readonly property bool armed: serviceState === "active"

  readonly property string icon: {
    if (!armed) return "󰍭"          // mic-off
    if (pipeline === "listening") return "󰋎"
    if (pipeline === "thinking") return "󰔟"
    if (pipeline === "speaking") return "󰕾"
    return "󰍬"                      // armed, waiting for the wake word
  }

  readonly property string stateLabel: {
    if (!armed) return "Disarmed"
    if (pipeline === "listening") return "Listening…"
    if (pipeline === "thinking") return "Thinking…"
    if (pipeline === "speaking") return "Speaking…"
    return "Armed — say \"hey jarvis\""
  }

  function refresh() {
    if (!stateProc.running) stateProc.running = true
  }

  function toggle() {
    if (busy) return
    busy = true
    controlProc.command = ["systemctl", "--user", armed ? "stop" : "start", unit]
    controlProc.running = true
  }

  function restart() {
    if (busy) return
    busy = true
    controlProc.command = ["systemctl", "--user", "restart", unit]
    controlProc.running = true
  }

  function sendNote(body) {
    if (!notify || !bar) return
    bar.run("notify-send -a Jarvis " + bar.shellQuote("Voice assistant") + " " + bar.shellQuote(body))
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // One cheap call for both halves of the state: unit status, then the
  // daemon's own pipeline marker.
  Process {
    id: stateProc
    command: ["sh", "-c",
      "systemctl --user is-active " + root.unit +
      " 2>/dev/null; cat \"${XDG_RUNTIME_DIR:-/tmp}/jarvis/state\" 2>/dev/null"]
    stdout: StdioCollector {
      id: stateOut
      waitForEnd: true
      onStreamFinished: {
        var lines = String(text || "").trim().split("\n")
        root.serviceState = (lines[0] || "unknown").trim()
        root.pipeline = root.serviceState === "active"
          ? (lines.length > 1 ? lines[1].trim() : "idle")
          : "off"
      }
    }
  }

  Process {
    id: controlProc
    onExited: function(exitCode) {
      root.busy = false
      root.refresh()
      if (exitCode !== 0) root.sendNote("systemctl failed (exit " + exitCode + ")")
      else root.sendNote(root.armed ? "Disarmed" : "Armed — say \"hey jarvis\"")
    }
  }

  Timer {
    interval: root.pollSeconds * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  IpcHandler {
    target: "dorian.voice"

    function toggle(): void { root.toggle() }
    function arm(): void { if (!root.armed) root.toggle() }
    function disarm(): void { if (root.armed) root.toggle() }
    function restart(): void { root.restart() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icon
    // Light up only while it is actually doing something, so an armed but
    // idle listener stays visually quiet.
    active: root.armed && root.pipeline !== "idle"
    fixedWidth: root.vertical ? -1 : 0
    fixedHeight: root.vertical ? root.barSize : -1
    tooltipText: root.stateLabel
      + (root.armed ? "\n~3% of one core while armed" : "")
      + "\nleft: " + (root.armed ? "disarm" : "arm") + " · right: restart"

    onPressed: function(b) {
      if (b === Qt.RightButton) root.restart()
      else root.toggle()
    }
  }
}

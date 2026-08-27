import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui

// Arm/disarm the "hey jarvis" wake-word listener.
//
// The listener is a systemd user unit that holds the mic open and runs
// openWakeWord continuously (~3% of one core), so it is off by default and
// this widget is the switch. Left click opens the settings panel, right click
// arms or disarms, middle click restarts.
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
    return "Armed: say \"hey jarvis\""
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
    if (!notify) return
    // argv, not a shell string: Bar has no shellQuote(), and building one by
    // hand is how a notification body ends up interpreted as shell.
    Quickshell.execDetached(["notify-send", "-a", "Jarvis", "Voice assistant", body])
  }

  // ---- settings panel ------------------------------------------------
  // The panel is a separate QML file loaded lazily and handed a reference
  // back to this widget, so it can read the live pipeline state and arm or
  // disarm without duplicating any of it.
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  // Named open/close, not openPanel/closePanel: Bar.findPanelWidget only
  // treats a widget as panel-bearing when it exposes open(), close() and
  // `opened`. Miss one and hotkeys and `omarchy-shell shell toggle` silently
  // skip the widget.
  function open()        { if (panelLoader.item) panelLoader.item.open() }
  function close()       { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { if (panelLoader.item) panelLoader.item.toggle() }

  readonly property bool popoutSwitchClosing:
    panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // One cheap call for both halves of the state: unit status, then the
  // daemon's own pipeline marker.
  Process {
    id: stateProc
    // Never read from /tmp: a predictable path in a world-writable directory
    // lets another local user plant `state` as a FIFO, and this poll runs
    // every second. Match the daemon's private fallback, bound the read, and
    // cap how long it may block if the file is a pipe anyway.
    // `unit` is a widget setting, so it is a string someone can type. It is
    // passed as a positional argument and referenced as "$1", never pasted
    // into the script text -- concatenating it would make a bar setting able
    // to carry its own shell.
    command: ["sh", "-c",
      "systemctl --user is-active \"$1\" 2>/dev/null; timeout 2 head -c 64 " +
      "\"${XDG_RUNTIME_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}}/jarvis/state\" 2>/dev/null",
      "jarvis-state", root.unit]
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
      else root.sendNote(root.armed ? "Disarmed" : "Armed: say \"hey jarvis\"")
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

    function arm(): void { if (!root.armed) root.toggle() }
    function disarm(): void { if (root.armed) root.toggle() }
    function toggleArmed(): void { root.toggle() }
    function restart(): void { root.restart() }
    function settings(): void { root.togglePanel() }
    function toggle(): void { root.togglePanel() }
    function open(): void { root.open() }
    function close(): void { root.close() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.icon
    // Light up only while it is actually doing something, so an armed but
    // idle listener stays visually quiet.
    active: (root.armed && root.pipeline !== "idle") || root.opened
    fixedWidth: root.vertical ? -1 : 0
    fixedHeight: root.vertical ? root.barSize : -1
    tooltipText: root.stateLabel
      + (root.armed ? "\n~3% of one core while armed" : "")
      + "\nleft: settings · right: " + (root.armed ? "disarm" : "arm")

    // Left opens the panel, matching every other bar widget. Arming stays one
    // click away on the right button, and from the switch inside the panel.
    onPressed: function(b) {
      if (b === Qt.RightButton) root.toggle()
      else if (b === Qt.MiddleButton) root.restart()
      else root.togglePanel()
    }
  }
}

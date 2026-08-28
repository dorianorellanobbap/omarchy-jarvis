import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Settings panel for the voice assistant.
//
// Jarvis's settings live in ~/.config/jarvis/config.toml, because the daemon
// that reads them is Python and knows nothing about the bar. QML has no TOML
// support, so every read and write goes through the `jarvis-config` helper:
// `show` hands back JSON, `set` rewrites a single line and keeps the comments.
//
// That helper also decides what is settable. It refuses to touch anything
// under [agents.*], so nothing here can rewrite the argv the daemon executes.
Panel {
  id: root
  moduleName: "dorian.voice"
  ipcTarget: "dorian.voice.panel"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var host: hostWidget || root

  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property bool armed: host && host.armed === true
  readonly property string pipeline: host ? host.pipeline : "off"

  // Mirrors of config.toml, refilled by `jarvis-config show` every time the
  // panel opens. Empty until the first load lands.
  property string agent: ""
  property string wakeWord: ""
  property var agents: []
  property var wakeWords: []
  property real wakeThreshold: 0.5
  property real silenceTail: 1.2
  property real maxCommand: 15.0
  property var voices: []
  property string voice: ""
  property bool loaded: false
  property string errorText: ""

  // A voice is ~63MB, so selecting one that is not on disk yet downloads it
  // first. The dropdown locks while that runs.
  property bool installingVoice: false
  property string pendingVoice: ""

  // The daemon reads its config once at startup, so a change only takes
  // effect on restart. Restarting a disarmed listener would arm it, which is
  // never what someone wants from a settings panel, so it stays disarmed and
  // the pending note explains why.
  property bool pendingRestart: false

  function open()   { load(); root.controller.show() }
  function close()  { root.controller.hide() }
  function toggle() { root.opened ? root.close() : root.open() }

  function load() {
    errorText = ""
    showProc.running = true
  }

  function agentEntry(name) {
    for (var i = 0; i < agents.length; i++)
      if (agents[i].name === name) return agents[i]
    return null
  }

  readonly property string agentNote: {
    var e = agentEntry(agent)
    if (!e) return ""
    if (!e.installed) return "not installed, replies will fail"
    return e.actions ? "can open apps and URLs" : "answer-only"
  }

  // config.toml stores a filename; the dropdown works in catalog ids.
  readonly property string voiceId: root.voice.replace(/\.onnx$/, "")

  function voiceEntry(id) {
    for (var i = 0; i < voices.length; i++)
      if (voices[i].id === id) return voices[i]
    return null
  }

  function chooseVoice(id) {
    if (id === root.voiceId || root.installingVoice) return
    var entry = voiceEntry(id)
    if (entry && entry.installed) {
      root.apply("voice", entry.file)
      return
    }
    root.errorText = ""
    root.pendingVoice = id
    root.installingVoice = true
    installProc.command = [root.helper, "install-voice", id]
    installProc.running = true
  }

  // One place for every write, so the restart bookkeeping cannot drift.
  function apply(key, value) {
    errorText = ""
    setProc.command = [root.helper, "set", key, String(value)]
    setProc.running = true
  }

  function onApplied() {
    load()
    if (armed) {
      pendingRestart = false
      restartProc.running = true
    } else {
      pendingRestart = true
    }
  }

  Process {
    id: showProc
    command: [root.helper, "show"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var d = JSON.parse(String(text || "{}"))
          root.agent = d.agent || ""
          root.wakeWord = d.wake_word || ""
          root.agents = d.agents || []
          root.wakeWords = d.wake_words || []
          root.voices = d.voices || []
          root.voice = d.voice || ""
          if (d.listen) {
            root.wakeThreshold = d.listen.wake_threshold
            root.silenceTail = d.listen.silence_tail
            root.maxCommand = d.listen.max_command
          }
          root.loaded = true
        } catch (e) {
          root.errorText = "Could not read the config file."
        }
      }
    }
    onExited: function(code) {
      if (code !== 0) root.errorText = "jarvis-config failed (exit " + code + ")"
    }
  }

  Process {
    id: setProc
    // jarvis-config reports refusals on stderr; surface them rather than
    // silently leaving a control showing a value that was never written.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var msg = String(text || "").replace(/^\[jarvis\].*$/gm, "").trim()
        if (msg) root.errorText = msg
      }
    }
    onExited: function(code) {
      if (code === 0) root.onApplied()
      else { if (!root.errorText) root.errorText = "Could not save that setting."; root.load() }
    }
  }

  Process {
    id: installProc
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var msg = String(text || "").replace(/^\[jarvis\].*$/gm, "").trim()
        if (msg) root.errorText = msg
      }
    }
    onExited: function(code) {
      root.installingVoice = false
      if (code === 0) {
        root.apply("voice", root.pendingVoice + ".onnx")
      } else if (!root.errorText) {
        root.errorText = "Could not download that voice."
      }
      root.pendingVoice = ""
    }
  }

  Process {
    id: restartProc
    command: ["systemctl", "--user", "restart", "jarvis"]
    onExited: function(code) {
      if (code !== 0) root.errorText = "Saved, but restarting the listener failed."
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.host
    bar: root.bar
    open: root.opened
    centerOnBar: false
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(380))
    // The cap is what the card is allowed to grow to, not what the content
    // needs. At 560 the hero, the three pickers, the three sliders and the
    // buttons together overran it, and everything past the cap was simply
    // clipped -- the buttons were unreachable. This fits the lot on a normal
    // screen; fittedContentHeight still clamps to the space the bar leaves,
    // and the Flickable below makes whatever a short screen cuts scrollable
    // rather than lost.
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: if (root.host && root.host.toggle) root.host.toggle()
      onMoveRequested: function(dx, dy) {
        if (dy === 0) return
        var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
        panelFlick.contentY = Math.max(0, Math.min(maxY,
                                panelFlick.contentY + dy * Style.space(56)))
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        // A panel always opens at the top. Without this the Flickable keeps
        // whatever contentY it was left at, and a reopen starts mid-card
        // with the hero scrolled off.
        Connections {
          target: root
          function onOpenedChanged() { if (root.opened) panelFlick.contentY = 0 }
        }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(14)

          // ------------------------------------------------------------ hero
          Item {
            width: parent.width
            implicitHeight: Math.max(heroIcon.implicitHeight, heroText.implicitHeight,
                                     heroSwitch.implicitHeight)

            Text {
              id: heroIcon
              text: root.host ? root.host.icon : "󰍭"
              color: root.armed ? root.accent : root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroText
              anchors.left: heroIcon.right
              anchors.leftMargin: Style.space(12)
              anchors.right: heroSwitch.left
              anchors.rightMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "Voice Assistant"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: parent.width
              }
              Text {
                text: root.host ? root.host.stateLabel : ""
                color: root.armed ? root.accent : Qt.darker(root.fg, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
                width: parent.width
              }
            }

            ToggleSwitch {
              id: heroSwitch
              checked: root.armed
              foreground: root.fg
              accent: root.accent
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              onToggled: if (root.host && root.host.toggle) root.host.toggle()

              PanelToolTip {
                visible: heroSwitch.containsMouse
                text: root.armed ? "Disarm the listener" : "Arm the listener"
                fontFamily: root.fontFamily
              }
            }
          }

          // Only shown when it matters: a saved change the daemon has not read.
          BorderSurface {
            width: parent.width
            visible: root.pendingRestart && !root.armed
            implicitHeight: pendingText.implicitHeight + Style.space(16)
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.fg, root.accent)
            borderSpec: Border.controlSpec("normal", root.fg, root.accent)

            Text {
              id: pendingText
              anchors.centerIn: parent
              width: parent.width - Style.space(20)
              text: "Saved. Takes effect when you arm the listener."
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              horizontalAlignment: Text.AlignHCenter
            }
          }

          BorderSurface {
            width: parent.width
            visible: root.errorText !== ""
            implicitHeight: errText.implicitHeight + Style.space(16)
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.fg, root.accent)
            borderSpec: Border.controlSpec("hover-cursor", root.fg, root.accent)

            Text {
              id: errText
              anchors.centerIn: parent
              width: parent.width - Style.space(20)
              text: root.errorText
              color: root.fg
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              horizontalAlignment: Text.AlignHCenter
            }
          }

          PanelSeparator { foreground: root.fg }

          // -------------------------------------------------------- behaviour
          PanelSectionHeader {
            text: "ASSISTANT"
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          Column {
            width: parent.width
            spacing: Style.space(4)

            Dropdown {
              width: parent.width
              label: "Agent"
              value: root.agent
              enabled: root.loaded
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              options: {
                var out = []
                for (var i = 0; i < root.agents.length; i++) {
                  var a = root.agents[i]
                  out.push({
                    value: a.name,
                    label: a.name + (a.installed ? "" : "  (not installed)")
                  })
                }
                return out
              }
              onChanged: function(v) { if (v !== root.agent) root.apply("agent", v) }
            }

            Text {
              text: root.agentNote
              visible: text !== ""
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Dropdown {
            width: parent.width
            label: "Wake word"
            value: root.wakeWord
            enabled: root.loaded
            foreground: root.fg
            accent: root.accent
            fontFamily: root.fontFamily
            options: {
              var out = []
              for (var i = 0; i < root.wakeWords.length; i++) {
                var w = root.wakeWords[i]
                out.push({ value: w, label: w.replace(/_/g, " ") })
              }
              return out
            }
            onChanged: function(v) { if (v !== root.wakeWord) root.apply("wake_word", v) }
          }

          Column {
            width: parent.width
            spacing: Style.space(4)

            Dropdown {
              width: parent.width
              label: "Voice"
              value: root.voiceId
              enabled: root.loaded && !root.installingVoice
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              options: {
                var out = []
                for (var i = 0; i < root.voices.length; i++) {
                  var v = root.voices[i]
                  out.push({
                    value: v.id,
                    label: v.id.replace(/^en_/, "").replace(/-medium$/, "").replace(/_/g, " ")
                           + (v.installed ? "" : "  (download)")
                  })
                }
                return out
              }
              onChanged: function(v) { root.chooseVoice(v) }
            }

            Text {
              text: root.installingVoice
                ? "Downloading " + root.pendingVoice + "… about 63 MB."
                : "Voices not listed here work too. Put a path in the config file."
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              width: parent.width
            }
          }

          PanelSeparator { foreground: root.fg }

          // --------------------------------------------------------- listening
          PanelSectionHeader {
            text: "LISTENING"
            foreground: root.fg
            fontFamily: root.fontFamily
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              implicitHeight: sensLabel.implicitHeight

              Text {
                id: sensLabel
                text: "Sensitivity"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                anchors.left: parent.left
              }
              Text {
                text: root.wakeThreshold.toFixed(2)
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
              }
            }

            // Lower threshold = fires more easily, so the slider is inverted to
            // read left-to-right as "less sensitive" -> "more sensitive".
            PanelSlider {
              width: parent.width
              bar: root.bar
              enabled: root.loaded
              value: 1.0 - root.wakeThreshold
              minimum: 0.05
              maximum: 0.9
              step: 0.05
              onReleased: function(v) { root.apply("listen.wake_threshold", (1.0 - v).toFixed(2)) }
            }

            Text {
              text: "Higher picks up the wake word more readily, and misfires more."
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              width: parent.width
            }
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              implicitHeight: tailLabel.implicitHeight

              Text {
                id: tailLabel
                text: "Pause before it answers"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                anchors.left: parent.left
              }
              Text {
                text: root.silenceTail.toFixed(1) + "s"
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
              }
            }

            PanelSlider {
              width: parent.width
              bar: root.bar
              enabled: root.loaded
              value: root.silenceTail
              minimum: 0.4
              maximum: 3.0
              step: 0.1
              onReleased: function(v) { root.apply("listen.silence_tail", v.toFixed(1)) }
            }
          }

          Column {
            width: parent.width
            spacing: Style.space(6)

            Item {
              width: parent.width
              implicitHeight: maxLabel.implicitHeight

              Text {
                id: maxLabel
                text: "Longest question"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                anchors.left: parent.left
              }
              Text {
                text: Math.round(root.maxCommand) + "s"
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.right: parent.right
              }
            }

            PanelSlider {
              width: parent.width
              bar: root.bar
              enabled: root.loaded
              value: root.maxCommand
              minimum: 5
              maximum: 60
              step: 5
              integer: true
              onReleased: function(v) { root.apply("listen.max_command", Math.round(v)) }
            }
          }

          PanelSeparator { foreground: root.fg }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              id: restartBtn
              width: Math.floor((parent.width - Style.space(8)) / 2)
              text: "Restart"
              iconText: "󰑖"
              bordered: true
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: {
                root.pendingRestart = false
                restartProc.running = true
              }
            }

            Button {
              width: parent.width - restartBtn.width - Style.space(8)
              text: "Edit config"
              iconText: "󰈙"
              bordered: true
              foreground: root.fg
              accent: root.accent
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              // Not xdg-open: config.toml is text/plain, whose handler here
              // is a terminal editor with Terminal=true, and execDetached
              // gives it no terminal to run in. The result was an invisible
              // nvim per click, piling up unreachable in the background.
              // omarchy-launch-config-editor opens the user's chosen editor,
              // wrapping a TUI one in a terminal, and toasts what it opened.
              onClicked: {
                Quickshell.execDetached(
                  ["omarchy-launch-config-editor", root.configPath])
                root.close()
              }
            }
          }

          Text {
            width: parent.width
            text: "Everything else (adding an agent, the voice) lives in the config file."
            color: Qt.darker(root.fg, 1.4)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }

  readonly property string configPath:
    (Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config"))
    + "/jarvis/config.toml"

  // Absolute, because omarchy-shell's PATH is not ours to assume and the
  // helper has to run under the venv's interpreter.
  readonly property string helper:
    Quickshell.env("HOME") + "/.local/share/jarvis/bin/jarvis-config"
}

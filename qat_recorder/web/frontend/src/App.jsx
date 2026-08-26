import { useState, useEffect, useRef, useCallback } from 'react';
import { api, connectEvents, mediaUrl, fetchMediaDataUrl } from './api';
import { loadSettings, saveSettings, chooseCandidate } from './llm';
import LlmSettings from './components/LlmSettings';
import MediaImage from './components/MediaImage';
import Header from './components/Header';
import ConnectBar from './components/ConnectBar';
import SetupBar from './components/SetupBar';
import Toolbar from './components/Toolbar';
import ActionTable from './components/ActionTable';
import TestLibrary from './components/TestLibrary';
import DetailsPane from './components/DetailsPane';
import CounterBar from './components/CounterBar';
import CheckpointModal from './components/CheckpointModal';
import KeepModal from './components/KeepModal';
import LiveScriptEditor from './components/LiveScriptEditor';

// States mirror qat_recorder.ui.controller.State
const S = { IDLE: 'idle', RECORDING: 'recording', PAUSED: 'paused', PICKING: 'picking', STOPPED: 'stopped' };

export default function App() {
  // ── connection ─────────────────────────────────────
  const [agentUrl, setAgentUrl] = useState('');   // e.g. "https://abc.ngrok.io" or "http://192.168.1.50:8765"
  const [token, setToken]       = useState('');
  const [connected, setConnected] = useState(false);
  const [agentHost, setAgentHost] = useState('');
  const [build, setBuild] = useState(null);
  // A still opened full size. The thumbnail in a gap is too small to
  // recognise a dialog by.
  const [lightbox, setLightbox] = useState(null);
  const [llm, setLlm] = useState(loadSettings);
  const [showLlmSettings, setShowLlmSettings] = useState(false);
  // What the model said about one gap, awaiting the operator's yes.
  const [proposal, setProposal] = useState(null);

  // ── session state ──────────────────────────────────
  const [state, setState]         = useState(S.IDLE);
  const [sessionId, setSessionId] = useState('');
  const [actions, setActions]     = useState([]);
  const [summary, setSummary]     = useState({});
  const [selectedRow, setSelectedRow] = useState(-1);
  const [details, setDetails]     = useState('Connect to an agent to begin');
  const [statusMsg, setStatusMsg] = useState('Not connected');

  // ── setup fields ───────────────────────────────────
  const [appPath, setAppPath]   = useState('');
  const [libPath, setLibPath]   = useState('');
  const [appName, setAppName]   = useState('');

  // ── tabs ───────────────────────────────────────────
  const [activeTab, setActiveTab] = useState('session');
  const [rightTab, setRightTab]   = useState('details');

  // ── script & dropped state ──────────────────────────
  const [scriptText, setScriptText] = useState('');
  const [customScript, setCustomScript] = useState('');
  const [isScriptEdited, setIsScriptEdited] = useState(false);
  const [droppedEvents, setDroppedEvents] = useState([]);
  const [gaps, setGaps] = useState([]);
  const [media, setMedia] = useState({ stills: [], video: '', gap_shots: {} });
  
  // ── modals ───────────────────────────────────────────
  const [showCodeModal, setShowCodeModal] = useState(false);
  const [customCodeInput, setCustomCodeInput] = useState('');
  // Set when the code modal was opened to fill a particular gap rather than to
  // append a step at the end.
  const [repairTarget, setRepairTarget] = useState(null);
  // Bumped whenever something changes the recording behind the panel's back, so
  // the preview is fetched again.
  const [refreshTick, setRefreshTick] = useState(0);

  // ── library ────────────────────────────────────────
  const [tests, setTests]       = useState([]);
  const [testResults, setTestResults] = useState({});
  const [selectedTest, setSelectedTest] = useState(-1);

  // ── modals ─────────────────────────────────────────
  const [checkpointData, setCheckpointData] = useState(null);
  const [showKeepModal, setShowKeepModal]   = useState(false);
  const [busy, setBusy] = useState(false);

  // ── split pane ─────────────────────────────────────
  const [leftWidth, setLeftWidth] = useState(58);

  const wsRef = useRef(null);
  const gapsRef = useRef(null);

  // ── helpers ────────────────────────────────────────
  const isStep = (a) => a.kind !== 'launch';
  // Gaps nobody has filled yet. The badge on the Live Script tab is the only
  // thing that tells the operator to go and look, since they spent the session
  // watching the application rather than this panel.
  const openGaps = gaps.filter(gap => !gap.repaired).length;
  const updateStatus = useCallback((msg) => setStatusMsg(msg), []);
  const shotFor = (index) => {
    const name = media.gap_shots?.[String(index)];
    return name ? { base: agentUrl, sid: sessionId, name, token } : null;
  };
  // The actions table is indexed over the steps the panel shows, which leaves
  // out the launch; the recording counts it. Hence the offset.
  const shotForStep = (row) => {
    const name = media.step_shots?.[String(row + 1)];
    return name ? { base: agentUrl, sid: sessionId, name, token } : null;
  };

  // ── connect to agent ───────────────────────────────
  const handleConnect = async (url, tok) => {
    const cleanUrl = url.replace(/\/+$/, '');  // strip trailing slashes
    setAgentUrl(cleanUrl);
    setToken(tok);
    try {
      const d = await api.health(cleanUrl, tok);
      setConnected(true);
      setAgentHost(d.host || 'agent');
      // Which build is actually running over there. A VM on an older wheel than
      // the panel expects makes every feature look broken at once, and nothing
      // used to say so.
      //
      // Only worth comparing when this page came from that same agent. Running
      // `qat-recorder web-panel` on your own machine and connecting to a remote
      // agent means two installs and two bundles by design -- warning about
      // that accused correctly installed machines of being out of date, every
      // single time they connected.
      const running = document.querySelector('script[src*="/static/assets/index-"]');
      const mine = running ? running.getAttribute('src').split('/').pop() : '';
      let servedByThisAgent = false;
      try {
        servedByThisAgent = new URL(cleanUrl).origin === window.location.origin;
      } catch { /* an unparseable URL is not this origin */ }
      setBuild({
        agent: d.version || '?',
        ui: d.ui_build || '',
        mine,
        servedByThisAgent,
        mismatch: servedByThisAgent && !!(mine && d.ui_build && mine !== d.ui_build),
      });
      if (d.default_lib) setLibPath(prev => prev || d.default_lib);
      
      // The agent may already be holding a recording -- the browser was
      // reloaded, or a second tab opened. Rejoin it, rather than leaving the
      // panel blank and answering Record with "a recording is in progress".
      if (d.session?.session_id) {
        setSessionId(d.session.session_id);
        setState(d.session.state || S.RECORDING);
        setAppName(prev => prev || d.session.app || '');
        connectWs(d.session.session_id);
        setRefreshTick(tick => tick + 1);
        updateStatus(`Rejoined the recording already running on ${d.host || 'the agent'}`);
      } else {
        updateStatus(`Connected to ${d.host || cleanUrl}`);
      }
      // Both builds, plainly, so a report of "it is behaving oddly" can quote
      // them instead of us guessing which machine is out of date.
      setDetails(`Connected to ${d.host || 'agent'}\n`
               + `qat_recorder ${d.version || '?'}  ·  protocol v${d.protocol || '?'}\n`
               + `UI on the agent: ${d.ui_build || 'not built'}\n`
               + `UI in this page: ${mine || 'unknown'}`);
    } catch (e) {
      setConnected(false);
      setAgentHost('');
      updateStatus(`Connection failed: ${e.message}`);
      setDetails(`Could not reach ${cleanUrl}\n\n${e.message}\n\nMake sure the agent is running.`);
    }
  };

  const handleDisconnect = () => {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    setConnected(false);
    setAgentUrl('');
    setAgentHost('');
    setState(S.IDLE);
    setSessionId('');
    setActions([]);
    setSummary({});
    setDetails('Connect to an agent to begin');
    updateStatus('Disconnected');
  };

  // ── WebSocket management ───────────────────────────
  const connectWs = useCallback((sid) => {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }

    const ws = connectEvents(agentUrl, sid, token, (msg) => {
      if (msg.state) setState(msg.state);
      if (msg.summary) {
        setSummary(msg.summary);
        // A gap closing is the one change that happens without the panel
        // asking for it -- the operator filled it by pointing at the control
        // in the application. Fetch the script again so it shows.
        if (gapsRef.current !== null && msg.summary.gaps !== gapsRef.current) {
          setRefreshTick(tick => tick + 1);
        }
        gapsRef.current = msg.summary.gaps ?? null;
      }

      if (msg.actions && msg.actions.length > 0) {
        setActions(prev => {
          const next = [...prev];
          msg.actions.forEach(a => { if (isStep(a)) next.push(a); });
          return next;
        });
      }

      if (msg.picked) {
        setCheckpointData({
          label: msg.picked.label || '—',
          properties: msg.picked.properties || {},
        });
      }

      if (msg.errors && msg.errors.length > 0) {
        updateStatus(msg.errors[msg.errors.length - 1]);
      }
    }, () => {
      setTimeout(() => {
        if (wsRef.current === ws) connectWs(sid);
      }, 2000);
    });

    wsRef.current = ws;
  }, [agentUrl, token, updateStatus]);

  useEffect(() => () => { if (wsRef.current) wsRef.current.close(); }, []);

  // ── toolbar actions ────────────────────────────────
  const startRecording = async () => {
    try {
      setActions([]);
      setSelectedRow(-1);
      setDetails('');
      setSummary({});
      setScriptText('');
      setCustomScript('');
      setIsScriptEdited(false);
      setDroppedEvents([]);
      const res = await api.startSession(agentUrl,
        { app: appPath, lib: libPath, name: appName || appPath.split('/').pop(), owner: 'web-panel' },
        token
      );
      setSessionId(res.session_id);
      setState(res.state || S.RECORDING);
      connectWs(res.session_id);
      updateStatus('Recording — interact with the application');
    } catch (e) {
      updateStatus(`Could not start recording: ${e.message}`);
    }
  };

  const sendCommand = async (cmd, args) => {
    if (!sessionId) return;
    try {
      const res = await api.command(agentUrl, sessionId, cmd, args, token);
      if (res.state) setState(res.state);
      if (res.summary) setSummary(res.summary);
      return res;
    } catch (e) {
      updateStatus(e.message);
    }
  };

  const togglePause = async () => {
    if (state === S.PAUSED) {
      await sendCommand('resume');
      updateStatus('Recording');
    } else {
      await sendCommand('pause');
      updateStatus('Paused — anything you do now is discarded');
    }
  };

  const stopRecording = async () => {
    await sendCommand('stop');
    updateStatus(`Stopped — ${summary.actions || actions.length} step(s) recorded`);
  };

  const armCheckpoint = async () => {
    await sendCommand('arm_checkpoint');
    updateStatus('Click the object you want to check — that click is not recorded');
  };

  const submitCheckpoint = async (property, expected) => {
    setCheckpointData(null);
    await sendCommand('add_checkpoint', { property, expected });
    updateStatus(`Checkpoint on ${property}`);
  };

  const undoLast = async () => {
    await sendCommand('undo');
    setActions(prev => prev.slice(0, -1));
    updateStatus('Removed last step');
  };

  const saveSession = async () => {
    if (!sessionId) return;
    try {
      setBusy(true);
      const scriptToSave = isScriptEdited ? customScript : null;
      const res = await api.artifacts(agentUrl, sessionId, scriptToSave, token);
      const fileCount = Object.keys(res.files || {}).length;
      // Written on the agent, beside the application. Nothing is downloaded:
      // the browser never sees these files.
      updateStatus(`Wrote ${fileCount} file(s) on ${agentHost || 'the agent'}: ${res.directory || 'session directory'}`);
    } catch (e) {
      updateStatus(`Could not save: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const keepSession = async (name) => {
    setShowKeepModal(false);
    if (!sessionId || !name) return;
    try {
      setBusy(true);
      updateStatus('Keeping, and running it once to check…');
      const res = await api.keep(agentUrl, sessionId, name.trim(), true, token);
      const verdict = res.test?.verified || '';
      updateStatus(`Kept as ${res.test?.name || name} — replayed ${verdict}`);
      refreshTests();
      setActiveTab('library');
    } catch (e) {
      updateStatus(`Could not keep this test: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const replaySession = async () => {
    if (!sessionId) return;
    try {
      setBusy(true);
      updateStatus('Replaying…');
      const res = await api.replay(agentUrl, sessionId, 0, token);
      const verdict = res.ok ? 'passed' : 'FAILED';
      updateStatus(`Replay ${verdict}`);
      setDetails(
        `replay ${verdict}   (exit code ${res.exit_code ?? '?'})\n` +
        `in ${res.directory || '?'}\n\n` +
        (res.output || '')
      );
    } catch (e) {
      updateStatus(`Could not replay: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  // ── library actions ────────────────────────────────
  const refreshTests = async () => {
    try {
      const res = await api.tests(agentUrl, appName, token);
      setTests(res.tests || []);
      updateStatus(`${(res.tests || []).length} saved test(s)`);
    } catch (e) {
      updateStatus(`Could not list tests: ${e.message}`);
    }
  };

  // A command answers with the envelope {protocol, state, summary}. It has no
  // "status" field: a failure arrives as a non-2xx, which api.js raises.
  const insertCustomCode = async () => {
    if (!sessionId || !customCodeInput.trim()) {
      setShowCodeModal(false);
      return;
    }
    try {
      if (repairTarget !== null) {
        await repairGap(repairTarget, customCodeInput.trim());
      } else {
        await api.command(agentUrl, sessionId, 'custom_code', { code: customCodeInput }, token);
        updateStatus('Step inserted');
      }
      setShowCodeModal(false);
      setCustomCodeInput('');
      setRepairTarget(null);
      setRefreshTick(tick => tick + 1);
    } catch (e) {
      updateStatus(`Could not insert that step: ${e.message}`);
    }
  };

  // The strongest fix available, and the one that needs no code at all: the
  // operator shows the recorder the control it could not name. The click goes
  // through the same resolver a recorded step goes through, so the step it
  // produces carries locators validated against the running application.
  // Ask the model which of the objects the recorder found is the one. It never
  // writes a locator -- it answers with an id from that list, and what gets
  // inserted is the Target the recorder itself resolved.
  const askModel = async (index) => {
    if (!sessionId) return;
    setProposal({ index, thinking: true });
    try {
      const pack = await api.evidence(agentUrl, sessionId, index, token);
      let picture = null;
      if (pack.shot) {
        try {
          picture = await fetchMediaDataUrl(agentUrl, sessionId, pack.shot, token);
        } catch { /* the evidence still stands without it */ }
      }
      // The steps on either side. Which control was meant is often obvious
      // from what came before it -- opening Preferences, then a tab -- and the
      // evidence pack on its own says nothing about that.
      const at = (pack.evidence?.after ?? actions.length);
      const around = [
        ...actions.slice(Math.max(0, at - 3), at)
                  .map(one => ({ before: true, kind: one.kind,
                                 label: one.target?.label })),
        ...actions.slice(at, at + 2)
                  .map(one => ({ before: false, kind: one.kind,
                                 label: one.target?.label })),
      ];
      const answer = await chooseCandidate(llm, pack.evidence || {}, {
        kind: pack.kind, label: pack.label, reason: pack.reason, around,
      }, picture);
      const candidate = (pack.evidence?.candidates || [])
        .find(one => one.id === answer.id);
      setProposal({ index, answer, candidate, pack });
      updateStatus(answer.id === null
        ? 'The model would not choose one — nothing in the list convinced it'
        : `The model suggests id ${answer.id} (${answer.confidence} confidence)`);
    } catch (e) {
      // Kept on the gap rather than dropped, so the failure is where the
      // operator is looking and the button is reachable again. Losing the
      // proposal entirely is how this looked like a button that vanishes.
      setProposal({ index, failed: e.message });
      updateStatus(`Could not ask the model: ${e.message}`);
    }
  };

  const acceptProposal = async () => {
    if (!proposal?.answer || proposal.answer.id === null) return;
    try {
      setBusy(true);
      await api.command(agentUrl, sessionId, 'repair_choose',
                        { index: proposal.index, candidate: proposal.answer.id },
                        token);
      setProposal(null);
      setIsScriptEdited(false);
      setCustomScript('');
      setRefreshTick(tick => tick + 1);
      updateStatus('Gap filled from the object the model chose');
    } catch (e) {
      updateStatus(`Could not fill that gap: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  // Pointing is two steps, because the gap is almost always noticed from a
  // different screen than the one it happened on. Arming pauses the recording
  // so getting back there costs nothing; the second step says the control is in
  // front of them and the click after it is the one that counts.
  const applyRepairState = (answer) => {
    if (answer?.state) setState(answer.state);
    if (answer?.summary) setSummary(answer.summary);
  };

  const pointAtGap = async (index) => {
    if (!sessionId) return;
    try {
      applyRepairState(await api.command(agentUrl, sessionId, 'arm_repair',
                                         { index }, token));
      updateStatus('Paused — walk back to that control; nothing is being recorded');
    } catch (e) {
      updateStatus(`Could not start pointing: ${e.message}`);
    }
  };

  const pickNow = async () => {
    if (!sessionId) return;
    try {
      applyRepairState(await api.command(agentUrl, sessionId, 'pick_now', {}, token));
      updateStatus('Ready — click that control in the application');
    } catch (e) {
      updateStatus(`Could not arm the pick: ${e.message}`);
    }
  };

  const cancelRepair = async () => {
    if (!sessionId) return;
    try {
      applyRepairState(await api.command(agentUrl, sessionId, 'cancel_repair',
                                         {}, token));
      updateStatus('Gave up on that gap. Recording is still paused — resume when the application is back where it was.');
    } catch (e) {
      updateStatus(`Could not cancel: ${e.message}`);
    }
  };

  // The one-click fix. It inserts the object the recorder identified and
  // checked at the moment the event was lost -- never a definition assembled
  // here out of what the filter happened to report, which is how six unnamed
  // check boxes became six copies of a step that matched all of them.
  const applyFix = async (index, text) => {
    if (!sessionId) return;
    try {
      setBusy(true);
      await api.command(agentUrl, sessionId, 'repair_suggestion', { index, text }, token);
      setIsScriptEdited(false);          // the recording is the truth again
      setCustomScript('');
      setRefreshTick(tick => tick + 1);
      updateStatus('Gap filled — the step is in the recording now');
    } catch (e) {
      updateStatus(`Could not fill that gap: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  // Filling a gap goes to the recording, not to the text in the pane. Keep
  // regenerates every artifact from the recording before it verifies, so a fix
  // that lived only in the editor would be thrown away by keeping it.
  const repairGap = async (index, code) => {
    if (!sessionId) return;
    try {
      setBusy(true);
      await api.command(agentUrl, sessionId, 'repair_drop', { index, code }, token);
      setIsScriptEdited(false);          // the recording is the truth again
      setCustomScript('');
      setRefreshTick(tick => tick + 1);
      updateStatus('Gap filled — the step is in the recording now');
    } catch (e) {
      updateStatus(`Could not fill that gap: ${e.message}`);
    } finally {
      setBusy(false);
    }
  };

  const runTest = async (caseData) => {
    try {
      setBusy(true);
      updateStatus(`Running: ${caseData.name}…`);
      const result = await api.runTest(agentUrl, caseData.id, 0, token);
      setTestResults(prev => ({ ...prev, [caseData.id]: result }));
      const v = result.ok ? 'passed' : 'FAILED';
      updateStatus(`${caseData.name}: ${v}`);
      setDetails(
        `${v === 'passed' ? 'PASS' : 'FAIL'}  ${caseData.name}\n\n` +
        (result.output || '')
      );
      refreshTests();
    } catch (e) {
      updateStatus(e.message);
    } finally {
      setBusy(false);
    }
  };

  const runAllTests = async () => {
    if (tests.length === 0) { updateStatus('No saved tests to run'); return; }
    let passed = 0;
    const lines = [];
    setBusy(true);
    for (let i = 0; i < tests.length; i++) {
      const c = tests[i];
      updateStatus(`Running ${i + 1}/${tests.length}: ${c.name}…`);
      try {
        const result = await api.runTest(agentUrl, c.id, 0, token);
        setTestResults(prev => ({ ...prev, [c.id]: result }));
        if (result.ok) passed++;
        lines.push(`${result.ok ? 'PASS' : 'FAIL'}  ${c.name}`);
        if (!result.ok) lines.push(`        ${result.output || ''}`);
      } catch (e) {
        lines.push(`FAIL  ${c.name}\n        ${e.message}`);
      }
    }
    setBusy(false);
    updateStatus(`${passed}/${tests.length} passed`);
    setDetails(`${passed} of ${tests.length} passed\n\n${lines.join('\n')}`);
    refreshTests();
  };

  // ── action row selection ───────────────────────────
  const selectAction = (idx) => {
    setSelectedRow(idx);
    const action = actions[idx];
    if (!action) { setDetails(''); return; }

    const lines = [
      `action      ${action.kind}`,
      `arguments   ${JSON.stringify(action.args)}`,
      `at          ${(action.t || 0).toFixed(2)}s`,
    ];
    if (action.target) {
      lines.push('', `object      ${action.target.label}`,
        `strategy    ${action.target.strategy}`,
        `durability  ${action.target.robustness}`,
        `definition  ${JSON.stringify(action.target.definition)}`);
      if (action.target.index != null)
        lines.push(`index       ${action.target.index}  (positional: order-dependent)`);
      if (action.target.warnings?.length)
        action.target.warnings.forEach(w => lines.push(`warning     ${w}`));
    }
    if (action.note) lines.push('', `note        ${action.note}`);
    setDetails(lines.join('\n'));
  };

  const selectTest = (idx) => {
    setSelectedTest(idx);
    const c = tests[idx];
    if (!c) return;
    const result = testResults[c.id];
    const lines = [
      `test        ${c.name}`,
      `application ${c.app}`,
      `steps       ${c.steps}`,
      `recorded    ${c.created}`,
      `folder      ${c.directory}`,
    ];
    if (c.needs_review) lines.push(`review      ${c.needs_review} step(s) are weak or fragile`);
    if (c.unresolved)   lines.push(`dropped     ${c.unresolved} event(s)`);
    if (result) {
      lines.push('', `last run    ${result.ok ? 'passed' : 'FAILED'}`, '', result.output || '');
    }
    setDetails(lines.join('\n'));
  };

  useEffect(() => {
    if (activeTab === 'library' && connected && tests.length === 0) refreshTests();
  }, [activeTab, connected]);

  // ── derived state ──────────────────────────────────
  const recording = state === S.RECORDING || state === S.PICKING;
  const idle      = state === S.IDLE || state === S.STOPPED;
  const setupCollapsed = !idle;

  useEffect(() => {
    if (!sessionId) return;
    api.preview(agentUrl, sessionId, token)
      .then(res => {
        setScriptText(res.script || '');
        setDroppedEvents(res.failures || []);
        setGaps(res.gaps || []);
      })
      .catch(e => console.error("Preview failed:", e));
    api.media(agentUrl, sessionId, token)
      .then(setMedia)
      .catch(() => {/* an agent too old to keep media is not an error */});
  }, [actions, sessionId, agentUrl, token, refreshTick]);

  return (
    <div className="app-shell">
      <Header connected={connected} state={state} agentHost={agentHost} />

      <ConnectBar
        connected={connected}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
      />

      {connected && (
        <>
          <SetupBar
            collapsed={setupCollapsed}
            appPath={appPath} libPath={libPath} appName={appName}
            onAppChange={setAppPath} onLibChange={setLibPath} onNameChange={setAppName}
            disabled={!idle}
          />

          <Toolbar 
            state={state} busy={busy}
            onRecord={startRecording} onPause={togglePause} onStop={stopRecording}
            onCheckpoint={armCheckpoint} onInsertCode={() => { setRepairTarget(null); setCustomCodeInput(''); setShowCodeModal(true); }}
                  onModelSettings={() => setShowLlmSettings(true)}
                  onScreenshot={async () => {
                    // sendCommand reports its own failure and answers with
                    // nothing; claiming success regardless is how a failed
                    // screenshot looked like a successful one.
                    const answer = await sendCommand('screenshot');
                    if (!answer) return;
                    setRefreshTick(tick => tick + 1);
                    updateStatus(`Screenshot taken on ${agentHost || 'the agent'}`);
                  }} onUndo={undoLast} 
            onSave={saveSession} onKeep={() => setShowKeepModal(true)} onReplay={replaySession} 
          />

          <div className="main-content">
            <div className="split-pane">
              <div className="pane pane-left" style={{ flex: `0 0 ${leftWidth}%` }}>
                <div className="tab-bar">
                  <button className={`tab-btn ${activeTab === 'session' ? 'active' : ''}`}
                          onClick={() => setActiveTab('session')}>
                    This session
                  </button>
                  <button className={`tab-btn ${activeTab === 'library' ? 'active' : ''}`}
                          onClick={() => setActiveTab('library')}>
                    Saved tests
                  </button>
                </div>

                <div className={`tab-content ${activeTab === 'session' ? 'active' : ''}`}>
                  <ActionTable
                    actions={actions}
                    selectedRow={selectedRow}
                    onSelect={selectAction}
                    shotForStep={shotForStep}
                    onOpenShot={setLightbox}
                  />
                </div>

                <div className={`tab-content ${activeTab === 'library' ? 'active' : ''}`}>
                  <TestLibrary
                    tests={tests}
                    testResults={testResults}
                    selectedTest={selectedTest}
                    onSelect={selectTest}
                    onRefresh={refreshTests}
                    onRunOne={() => selectedTest >= 0 && tests[selectedTest] && runTest(tests[selectedTest])}
                    onRunAll={runAllTests}
                    disabled={recording || state === S.PAUSED}
                  />
                </div>
              </div>

              <ResizeHandle onResize={(pct) => setLeftWidth(pct)} />

              <div className="pane pane-right">
                <div className="tab-bar">
                  <button className={`tab-btn ${rightTab === 'details' ? 'active' : ''}`}
                          onClick={() => setRightTab('details')}>
                    Details
                  </button>
                  <button className={`tab-btn ${rightTab === 'script' ? 'active' : ''}`}
                          onClick={() => setRightTab('script')}>
                    Live Script
                    {openGaps > 0 && <span className="badge badge-muted" style={{marginLeft: 6, color: 'var(--color-unresolved)'}}>{openGaps}</span>}
                  </button>
                  <button className={`tab-btn ${rightTab === 'json' ? 'active' : ''}`}
                          onClick={() => setRightTab('json')}>
                    Live JSON
                  </button>
                  <button className={`tab-btn ${rightTab === 'screen' ? 'active' : ''}`}
                          onClick={() => setRightTab('screen')}>
                    Screen
                    {media.stills?.length > 0 && <span className="badge badge-muted" style={{marginLeft: 6}}>{media.stills.length}</span>}
                  </button>
                  <button className={`tab-btn ${rightTab === 'dropped' ? 'active' : ''}`}
                          onClick={() => setRightTab('dropped')}>
                    Dropped Events {droppedEvents.length > 0 && <span className="badge badge-muted" style={{marginLeft: 6, color: 'var(--color-unresolved)'}}>{droppedEvents.length}</span>}
                  </button>
                </div>

                <div className={`tab-content ${rightTab === 'details' ? 'active' : ''}`} style={{ display: rightTab === 'details' ? 'flex' : 'none', flex: 1, overflow: 'auto' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100%' }}>
                    <DetailsPane content={details} />
                  </div>
                </div>

                <div className={`tab-content ${rightTab === 'json' ? 'active' : ''}`} style={{ display: rightTab === 'json' ? 'flex' : 'none', flex: 1, overflow: 'auto', padding: 16 }}>
                   <pre style={{ margin: 0, fontFamily: "'JetBrains Mono', monospace", fontSize: '12px', color: 'var(--text-secondary)' }}>
                     {JSON.stringify({ actions }, null, 2)}
                   </pre>
                </div>

                <div className={`tab-content ${rightTab === 'script' ? 'active' : ''}`} style={{ display: rightTab === 'script' ? 'flex' : 'none', flex: 1, flexDirection: 'column', minHeight: 0 }}>
                  <LiveScriptEditor
                    script={isScriptEdited ? customScript : scriptText}
                    readOnly={recording}
                    busy={busy}
                    canPoint={state === S.RECORDING || state === S.PICKING}
                    picking={state === S.PICKING}
                    onPoint={pointAtGap}
                    onPickNow={pickNow}
                    onCancelPoint={cancelRepair}
                    arming={summary.arming ?? null}
                    onOpenShot={setLightbox}
                    llmReady={!!(llm.enabled && llm.apiKey)}
                    onAskModel={askModel}
                    proposal={proposal}
                    onAcceptProposal={acceptProposal}
                    onDismissProposal={() => setProposal(null)}
                    onCloseWindow={async (index) => {
                      try {
                        setBusy(true);
                        await api.command(agentUrl, sessionId, 'repair_closed',
                                          { index }, token);
                        setIsScriptEdited(false);
                        setCustomScript('');
                        setRefreshTick(tick => tick + 1);
                        updateStatus('Gap filled — close() on the name the window was closed under');
                      } catch (e) {
                        updateStatus(`Could not fill that gap: ${e.message}`);
                      } finally {
                        setBusy(false);
                      }
                    }}
                    onApply={applyFix}
                    shotFor={shotFor}
                    onWriteCode={(drop) => {
                      setRepairTarget(drop.index);
                      setCustomCodeInput('');
                      setShowCodeModal(true);
                    }}
                    onChange={(newScript) => {
                      setCustomScript(newScript);
                      setIsScriptEdited(true);
                    }}
                  />
                  {recording && <div style={{padding: '8px 16px', background: 'var(--bg-surface)', color: 'var(--color-weak)', fontSize: '11px', borderTop: '1px solid var(--border-subtle)'}}>Script is Read-Only while recording. Stop recording to edit manually before saving.</div>}
                  {!recording && !idle && <div style={{padding: '8px 16px', background: 'var(--bg-surface)', color: 'var(--color-strong)', fontSize: '11px', borderTop: '1px solid var(--border-subtle)'}}>Editable mode. Your changes will be saved to test_recorded.py</div>}
                </div>

                <div className={`tab-content screen-tab ${rightTab === 'screen' ? 'active' : ''}`} style={{ display: rightTab === 'screen' ? 'flex' : 'none' }}>
                  <div>
                    <div className="screen-heading">The session, filmed on {agentHost || 'the agent'}</div>
                    {media.video ? (
                      <video className="screen-video" controls preload="metadata"
                             src={mediaUrl(agentUrl, sessionId, media.video, token)} />
                    ) : (
                      <div className="screen-note">
                        {media.filming
                          ? 'Filming. The video is written as the session runs and can be played once it stops.'
                          : (media.video_note || 'No video for this session.')}
                      </div>
                    )}
                  </div>

                  <div>
                    <div className="screen-heading">
                      Stills ({media.stills?.length || 0}) — one at every gap, plus any you took
                    </div>
                    {media.still_note && (
                      <div className="screen-note">{media.still_note}</div>
                    )}
                    {media.stills?.length ? (
                      <div className="screen-strip">
                        {media.stills.map(name => (
                          <div key={name} title={name}>
                            <MediaImage base={agentUrl} sid={sessionId}
                                        name={name} token={token} alt={name} thumb />
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="screen-note">Nothing photographed yet.</div>
                    )}
                  </div>
                </div>

                <div className={`tab-content ${rightTab === 'dropped' ? 'active' : ''}`} style={{ display: rightTab === 'dropped' ? 'flex' : 'none', flex: 1, padding: 16, overflow: 'auto', flexDirection: 'column', gap: '8px' }}>
                  {droppedEvents.length === 0 ? (
                    <div className="empty-state" style={{flex: 'unset', marginTop: 40}}>
                      <svg width="48" height="48" viewBox="0 0 48 48" fill="none" opacity="0.3">
                        <circle cx="24" cy="24" r="20" stroke="currentColor" strokeWidth="2"/>
                        <path d="M16 24l6 6 10-10" stroke="currentColor" strokeWidth="2" fill="none"/>
                      </svg>
                      <p>No dropped events. Everything was recorded successfully!</p>
                    </div>
                  ) : (
                    droppedEvents.map((evt, i) => (
                      <div key={i} style={{ background: 'var(--bg-elevated)', padding: '12px', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-medium)' }}>
                        <div style={{ color: 'var(--color-unresolved)', marginBottom: 6, fontWeight: 'bold', fontSize: 11, textTransform: 'uppercase' }}>Dropped Event {i+1}</div>
                        <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: '12px', whiteSpace: 'pre-wrap', color: 'var(--text-secondary)' }}>{evt}</div>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </div>
          </div>
        </>
      )}

      {lightbox && (
        <div className="lightbox" onClick={() => setLightbox(null)}>
          <MediaImage {...lightbox} alt="what was on screen" />
          <button className="btn btn-secondary btn-sm lightbox-close"
                  onClick={() => setLightbox(null)}>Close</button>
        </div>
      )}

      {showLlmSettings && (
        <LlmSettings settings={llm} onClose={() => setShowLlmSettings(false)}
                     onSave={(next) => { setLlm(next); saveSettings(next); }} />
      )}

      {build?.mismatch && (
        <div className="build-warning">
          This page is running <code>{build.mine}</code> but the agent that
          served it now has <code>{build.ui}</code>. That is a stale cached page:
          reload with a hard refresh (Ctrl-Shift-R).
        </div>
      )}

      {!connected && (
        <div className="main-content">
          <div className="empty-state-full">
            <svg width="64" height="64" viewBox="0 0 28 28" fill="none" opacity="0.25">
              <circle cx="14" cy="14" r="12" stroke="currentColor" strokeWidth="2"/>
              <circle cx="14" cy="14" r="5" fill="currentColor"/>
            </svg>
            <h2>Connect to an agent</h2>
            <p>Enter the VM address or ngrok URL above to start recording.</p>
          </div>
        </div>
      )}

      <CounterBar summary={summary} statusMsg={statusMsg} />

      {checkpointData && (
        <CheckpointModal
          label={checkpointData.label}
          properties={checkpointData.properties}
          onSubmit={submitCheckpoint}
          onCancel={() => { setCheckpointData(null); updateStatus('Checkpoint cancelled'); }}
        />
      )}

      {showKeepModal && (
        <KeepModal
          onSubmit={keepSession}
          onCancel={() => setShowKeepModal(false)}
        />
      )}
      {/* Code Modal */}
      {showCodeModal && (
        <div className="modal-overlay" onClick={() => { setShowCodeModal(false); setRepairTarget(null); }}>
          <div className="modal" onClick={e => e.stopPropagation()} style={{minWidth: 600}}>
            <h2>{repairTarget !== null ? 'Fill this gap' : 'Insert a step'}</h2>
            <p>
              {repairTarget !== null
                ? 'This goes into the recording where the dropped event was, so every generated file gets it — not just the script in this pane.'
                : 'This is appended to the recording as a step of its own. It survives Save and Keep, because everything is generated from the recording.'}
            </p>
            <div className="modal-form">
              <label>Python Code</label>
              <textarea 
                value={customCodeInput}
                onChange={e => setCustomCodeInput(e.target.value)}
                placeholder={'qat.mouse_click({"text": "Hello"})'}
                spellCheck="false"
                style={{
                  padding: '12px', fontFamily: "'JetBrains Mono', monospace", 
                  backgroundColor: 'var(--bg-base)', color: 'var(--text-primary)', 
                  border: '1px solid var(--border-medium)', borderRadius: 'var(--radius-sm)',
                  resize: 'vertical', outline: 'none', fontSize: 13, minHeight: 120
                }}
              />
            </div>
            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={() => { setShowCodeModal(false); setRepairTarget(null); }}>Cancel</button>
              <button className="btn btn-primary" onClick={insertCustomCode} disabled={!customCodeInput.trim()}>
                {repairTarget !== null ? 'Fill the gap' : 'Insert step'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Resize Handle ─────────────────────────────────────────────
function ResizeHandle({ onResize }) {
  const handleRef = useRef(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    if (!active) return;
    const onMove = (e) => {
      const container = handleRef.current?.parentElement;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      onResize(Math.max(25, Math.min(75, pct)));
    };
    const onUp = () => setActive(false);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [active, onResize]);

  return (
    <div
      ref={handleRef}
      className={`resize-handle ${active ? 'active' : ''}`}
      onMouseDown={() => setActive(true)}
    />
  );
}

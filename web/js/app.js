/**
 * DEEP RED Stories — Narrated Game Replay Engine
 *
 * Loads a random game from a pre-built index, plays moves on a chessboard
 * synchronized with pre-generated TTS audio segments.
 */

(function () {
  'use strict';

  // ===== Configuration =====
  const DATA_BASE = 'data';

  // ===== DOM Elements =====
  const dom = {
    whiteName: document.getElementById('white-name'),
    blackName: document.getElementById('black-name'),
    event: document.getElementById('game-event'),
    date: document.getElementById('game-date'),
    eco: document.getElementById('game-eco'),
    result: document.getElementById('game-result'),
    moveList: document.getElementById('move-list'),
    narrativeText: document.getElementById('narrative-text'),
    btnPlay: document.getElementById('btn-play'),
    btnFastForward: document.getElementById('btn-fast-forward'),
    btnNewGame: document.getElementById('btn-new-game'),
    progressBar: document.getElementById('progress-bar'),
    moveCounter: document.getElementById('move-counter'),
    gameCounter: document.getElementById('game-counter'),
    chkAutoContinue: document.getElementById('chk-auto-continue'),
    loading: document.getElementById('loading-overlay'),
    boardEl: document.getElementById('board'),
    totalDuration: document.getElementById('total-duration'),
    segmentDuration: document.getElementById('segment-duration'),
  };

  // ===== State =====
  let state = {
    index: null,          // Master game index
    game: null,           // Current game.json data
    control: null,        // Current control.json data
    chess: null,          // chess.js instance
    board: null,          // chessboard.js instance
    playing: false,       // Is playback active
    currentPly: 0,        // Current half-move index (0-based)
    totalPlies: 0,        // Total half-moves
    segmentIdx: 0,        // Current segment being played
    moveTimer: null,      // setInterval handle for advancing moves
    audioElement: null,   // Current HTML5 Audio element
    flatMoves: [],        // Flat list of half-moves: [{san, moveNum, color}, ...]
    shuffledPlaylist: [], // Fisher-Yates shuffled game order
    playlistPos: -1,      // Current position in shuffled playlist
    autoContinueTimer: null, // Timer for auto-continue countdown
  };

  // ===== Sound Effects =====
  const whiteSounds = [
    new Audio('audio/click1.wav'),
    new Audio('audio/move1.wav'),
    new Audio('audio/move2.wav'),
    new Audio('audio/move3.wav'),
    new Audio('audio/move4.wav'),
  ];
  const blackSounds = [
    new Audio('audio/click2.wav'),
    new Audio('audio/move5.wav'),
    new Audio('audio/move6.wav'),
    new Audio('audio/move7.wav'),
    new Audio('audio/move8.wav'),
  ];
  const kingFallSound = new Audio('audio/king_fall.wav');

  // Pre-set volume
  whiteSounds.forEach(s => { s.volume = 0.30; });
  blackSounds.forEach(s => { s.volume = 0.30; });
  kingFallSound.volume = 0.5;

  function playClockClick(color) {
    const pool = color === 'w' ? whiteSounds : blackSounds;
    const snd = pool[Math.floor(Math.random() * pool.length)];
    snd.currentTime = 0;
    snd.play().catch(() => {});
  }

  function playKingFall() {
    kingFallSound.currentTime = 0;
    kingFallSound.play().catch(() => {});
  }

  // ===== Helpers =====

  function showLoading() {
    dom.loading.classList.remove('hidden');
  }

  function hideLoading() {
    dom.loading.classList.add('hidden');
  }

  function formatDate(d) {
    const months = ['','January','February','March','April','May','June',
      'July','August','September','October','November','December'];
    const parts = d.split('.');
    const year = parts[0], month = parts[1], day = parts[2];
    const hasYear = year && !year.includes('?');
    const hasMonth = month && !month.includes('?');
    const hasDay = day && !day.includes('?');
    if (!hasYear) return d;
    const mName = hasMonth ? months[parseInt(month, 10)] || month : '';
    if (hasYear && hasMonth && hasDay) return parseInt(day, 10) + ' ' + mName + ' ' + year;
    if (hasYear && hasMonth) return mName + ' ' + year;
    return 'in ' + year;
  }

  async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Failed to load ${url}: ${resp.status}`);
    return resp.json();
  }

  /** Fisher-Yates (Knuth) shuffle — in-place. */
  function shuffleArray(arr) {
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr;
  }

  /** Flatten the moves array into a list of half-moves (plies). */
  function flattenMoves(moves) {
    const flat = [];
    for (const m of moves) {
      flat.push({ san: m.white, moveNum: m.num, color: 'w' });
      if (m.black) {
        flat.push({ san: m.black, moveNum: m.num, color: 'b' });
      }
    }
    return flat;
  }

  /** Count half-moves in a segment's move range. */
  function pliesInSegment(seg) {
    if (seg.start_move === 0 && seg.end_move === 0) {
      return 0; // Intro segment — no moves
    }
    let count = 0;
    for (const fm of state.flatMoves) {
      if (fm.moveNum >= seg.start_move && fm.moveNum <= seg.end_move) {
        count++;
      }
    }
    return Math.max(count, 1);
  }

  /** Determine which ply index a segment starts at. */
  function segmentStartPly(seg) {
    if (seg.start_move === 0 && seg.end_move === 0) return 0;
    for (let i = 0; i < state.flatMoves.length; i++) {
      if (state.flatMoves[i].moveNum >= seg.start_move) return i;
    }
    return state.flatMoves.length;
  }

  // ===== UI Rendering =====

  function renderGameInfo() {
    const g = state.game;
    dom.whiteName.textContent = '♔ ' + g.white;
    dom.blackName.textContent = '♚ ' + g.black;
    dom.event.textContent = g.event || '';
    dom.date.textContent = g.date ? formatDate(g.date) : '';
    dom.eco.textContent = g.eco || '';
    dom.result.textContent = g.result || '';
  }

  function renderMoveList() {
    dom.moveList.innerHTML = '';
    const moves = state.game.moves;

    for (const m of moves) {
      const row = document.createElement('div');
      row.className = 'move-row';
      row.dataset.moveNum = m.num;

      const numSpan = document.createElement('span');
      numSpan.className = 'move-num';
      numSpan.textContent = m.num + '.';

      const wSpan = document.createElement('span');
      wSpan.className = 'move-white';
      wSpan.textContent = m.white;
      wSpan.dataset.ply = ''; // Will be set below

      row.appendChild(numSpan);
      row.appendChild(wSpan);

      if (m.black) {
        const bSpan = document.createElement('span');
        bSpan.className = 'move-black';
        bSpan.textContent = m.black;
        row.appendChild(bSpan);
      }

      dom.moveList.appendChild(row);
    }

    // Assign ply indices to move spans
    let ply = 0;
    const rows = dom.moveList.querySelectorAll('.move-row');
    rows.forEach(row => {
      const wSpan = row.querySelector('.move-white');
      const bSpan = row.querySelector('.move-black');
      if (wSpan) wSpan.dataset.ply = ply++;
      if (bSpan) bSpan.dataset.ply = ply++;
    });
  }

  function highlightPly(plyIdx) {
    // Clear previous highlights
    dom.moveList.querySelectorAll('.active').forEach(el => el.classList.remove('active'));

    // Mark all plies up to current as played
    const allSpans = dom.moveList.querySelectorAll('.move-white, .move-black');
    allSpans.forEach(el => {
      const p = parseInt(el.dataset.ply);
      if (p < plyIdx) {
        el.classList.add('played');
      } else {
        el.classList.remove('played');
      }
      if (p === plyIdx) {
        el.classList.add('active');
        el.classList.add('played');
        // Scroll into view
        el.closest('.move-row').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    });

    // Update counter
    dom.moveCounter.textContent = `Move: ${plyIdx + 1}/${state.totalPlies}`;

    // Update progress bar
    const pct = state.totalPlies > 0 ? ((plyIdx + 1) / state.totalPlies * 100) : 0;
    dom.progressBar.style.width = pct + '%';
  }

  function setNarrative(text) {
    dom.narrativeText.textContent = text || '';
  }

  function formatDuration(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m + ':' + String(s).padStart(2, '0');
  }

  function updateTotalDuration() {
    if (!state.control || !state.control.segments) {
      dom.totalDuration.textContent = '';
      return;
    }
    const total = state.control.segments.reduce((sum, s) => sum + (s.duration_seconds || 0), 0);
    dom.totalDuration.textContent = '\u23F1 ' + formatDuration(total);
  }

  function updateSegmentDuration(seg) {
    if (!seg || !seg.duration_seconds) {
      dom.segmentDuration.textContent = '';
      return;
    }
    dom.segmentDuration.textContent = formatDuration(seg.duration_seconds);
  }

  function setPlayButton(playing) {
    dom.btnPlay.textContent = playing ? '⏸' : '▶';
  }

  function updateGameCounter() {
    const total = state.shuffledPlaylist.length;
    const current = total > 0 ? state.playlistPos + 1 : 0;
    dom.gameCounter.textContent = `Game: ${current}/${total}`;
  }

  function updateFastForwardButton() {
    if (!state.control || !state.playing) {
      dom.btnFastForward.disabled = true;
      return;
    }
    const totalSegments = state.control.segments.length;
    // Disable on the last segment or when not playing
    const isLast = state.segmentIdx >= totalSegments - 1;
    dom.btnFastForward.disabled = isLast;

    if (!isLast && totalSegments > 0) {
      const nextSeg = state.control.segments[state.segmentIdx + 1];
      if (nextSeg.start_move && nextSeg.start_move > 0) {
        dom.btnFastForward.title = `Skip to move ${nextSeg.start_move}`;
      } else {
        dom.btnFastForward.title = 'Fast Forward';
      }
    } else {
      dom.btnFastForward.title = 'Fast Forward';
    }
  }

  // ===== Playback Engine =====

  function stopPlayback() {
    state.playing = false;
    setPlayButton(false);
    updateFastForwardButton();
    if (state.moveTimer) {
      clearInterval(state.moveTimer);
      state.moveTimer = null;
    }
    if (state.audioElement) {
      state.audioElement.onended = null;
      state.audioElement.pause();
    }
  }

  function cancelAutoContinue() {
    if (state.autoContinueTimer) {
      clearTimeout(state.autoContinueTimer);
      state.autoContinueTimer = null;
    }
  }

  function onGameFinished() {
    playKingFall();
    const resultText = 'Analysis complete \u2014 ' + (state.game.result || '');

    if (dom.chkAutoContinue.checked && state.playlistPos < state.shuffledPlaylist.length - 1) {
      let countdown = 5;
      setNarrative(resultText + ` — Next game in ${countdown}s...`);

      function tick() {
        countdown--;
        if (countdown <= 0) {
          state.autoContinueTimer = null;
          loadNextGame();
          return;
        }
        setNarrative(resultText + ` — Next game in ${countdown}s...`);
        state.autoContinueTimer = setTimeout(tick, 1000);
      }

      state.autoContinueTimer = setTimeout(tick, 1000);
    } else if (dom.chkAutoContinue.checked) {
      setNarrative(resultText + ' — Playlist complete.');
    } else {
      setNarrative(resultText);
    }
  }

  function advanceMove() {
    if (state.currentPly >= state.totalPlies) {
      stopPlayback();
      onGameFinished();
      return;
    }

    const fm = state.flatMoves[state.currentPly];

    // Apply move to chess.js
    const result = state.chess.move(fm.san, { sloppy: true });
    if (!result) {
      console.warn(`Invalid move at ply ${state.currentPly}: ${fm.san}`);
      // Try to continue anyway
      state.currentPly++;
      return;
    }

    // Update board with animation
    state.board.position(state.chess.fen());

    // Clock click sound
    playClockClick(fm.color);

    // Highlight in move list
    highlightPly(state.currentPly);

    state.currentPly++;
  }

  /** Create an Audio element with explicit MIME type via <source>. */
  function createAudio(url) {
    const audio = document.createElement('audio');
    audio.preload = 'auto';
    const source = document.createElement('source');
    source.src = url;
    const ext = url.split('.').pop().toLowerCase();
    if (ext === 'mp3') source.type = 'audio/mpeg';
    else if (ext === 'wav') source.type = 'audio/wav';
    else if (ext === 'ogg') source.type = 'audio/ogg';
    audio.appendChild(source);
    audio.addEventListener('error', function() {
      console.error('Audio error:', audio.error?.code, audio.error?.message, 'src:', url);
    });
    return audio;
  }

  function playSegment(segIdx) {
    if (segIdx >= state.control.segments.length) {
      // All segments done — play remaining moves quickly if any
      if (state.currentPly < state.totalPlies) {
        const remaining = state.totalPlies - state.currentPly;
        const interval = 800; // ~0.8s per move for remaining
        state.moveTimer = setInterval(() => {
          if (state.currentPly >= state.totalPlies || !state.playing) {
            stopPlayback();
            onGameFinished();
            return;
          }
          advanceMove();
        }, interval);
      } else {
        stopPlayback();
        onGameFinished();
      }
      return;
    }

    state.segmentIdx = segIdx;
    updateFastForwardButton();
    const seg = state.control.segments[segIdx];

    // Show narrative text
    setNarrative(seg.text);
    updateSegmentDuration(seg);

    // Load and play audio
    const gameId = state.game.game_id;
    const audioUrl = `${DATA_BASE}/games/${gameId}/${seg.audio_file}`;

    state.audioElement = createAudio(audioUrl);
    const plies = pliesInSegment(seg);
    const isIntro = seg.start_move === 0 && seg.end_move === 0;

    if (isIntro || plies === 0) {
      // Intro segment: play audio, no moves
      state.audioElement.onended = () => {
        if (state.playing) playSegment(segIdx + 1);
      };
      state.audioElement.play().catch(e => console.warn('Audio play failed:', e));
      return;
    }

    // Calculate move timing
    const duration = seg.duration_seconds || 10;
    const timings = seg.move_timings;
    const hasTimings = timings && timings.length === plies;
    const uniformMs = (duration / plies) * 1000;

    // Determine target ply for this segment end
    const startPly = segmentStartPly(seg);
    const targetPly = startPly + plies;

    // Ensure we're at the right starting ply
    // (advance silently if we're behind)
    while (state.currentPly < startPly && state.currentPly < state.totalPlies) {
      advanceMove();
    }

    // Start audio
    state.audioElement.play().catch(e => console.warn('Audio play failed:', e));

    // Schedule moves with variable timing based on narrative text positions
    function scheduleMoveStep(plyOffset) {
      if (!state.playing) return;
      if (state.currentPly >= targetPly || state.currentPly >= state.totalPlies) {
        state.moveTimer = null;
        if (state.audioElement && !state.audioElement.ended) {
          state.audioElement.onended = () => {
            if (state.playing) playSegment(segIdx + 1);
          };
        } else {
          if (state.playing) playSegment(segIdx + 1);
        }
        return;
      }

      const delayMs = hasTimings
        ? timings[plyOffset] * duration * 1000
        : uniformMs;

      state.moveTimer = setTimeout(() => {
        if (!state.playing) return;
        advanceMove();
        scheduleMoveStep(plyOffset + 1);
      }, Math.max(delayMs, 50));
    }

    scheduleMoveStep(0);

    // If audio ends before all moves in segment, speed up remaining
    state.audioElement.onended = () => {
      if (!state.moveTimer) return; // Moves already finished
      clearTimeout(state.moveTimer);
      state.moveTimer = setInterval(() => {
        if (!state.playing || state.currentPly >= targetPly || state.currentPly >= state.totalPlies) {
          clearInterval(state.moveTimer);
          state.moveTimer = null;
          if (state.playing) playSegment(segIdx + 1);
          return;
        }
        advanceMove();
      }, 400);
    };
  }

  function resumeSegment() {
    const segIdx = state.segmentIdx;
    const seg = state.control.segments[segIdx];
    const isIntro = seg.start_move === 0 && seg.end_move === 0;
    const plies = pliesInSegment(seg);

    if (isIntro || plies === 0) {
      state.audioElement.onended = () => {
        if (state.playing) playSegment(segIdx + 1);
      };
      state.audioElement.play().catch(e => console.warn('Audio play failed:', e));
      return;
    }

    const startPly = segmentStartPly(seg);
    const targetPly = startPly + plies;
    const remainingPlies = targetPly - state.currentPly;

    if (remainingPlies <= 0) {
      // All moves for this segment already played, just wait for audio
      state.audioElement.onended = () => {
        if (state.playing) playSegment(segIdx + 1);
      };
      state.audioElement.play().catch(e => console.warn('Audio play failed:', e));
      return;
    }

    // Calculate timing based on remaining audio and move timings
    const remainingAudio = (seg.duration_seconds || 10) - state.audioElement.currentTime;
    const timings = seg.move_timings;
    const plyOffset = state.currentPly - startPly;
    const hasTimings = timings && timings.length === plies && plyOffset >= 0 && plyOffset < plies;

    // Resume audio
    state.audioElement.play().catch(e => console.warn('Audio play failed:', e));

    // Schedule remaining moves with timing data
    if (hasTimings) {
      const remainingTimings = timings.slice(plyOffset);
      const tSum = remainingTimings.reduce((a, b) => a + b, 0);

      function scheduleResumeStep(idx) {
        if (!state.playing) return;
        if (state.currentPly >= targetPly || state.currentPly >= state.totalPlies) {
          state.moveTimer = null;
          if (state.audioElement && !state.audioElement.ended) {
            state.audioElement.onended = () => { if (state.playing) playSegment(segIdx + 1); };
          } else {
            if (state.playing) playSegment(segIdx + 1);
          }
          return;
        }

        const delayMs = tSum > 0
          ? (remainingTimings[idx] / tSum) * remainingAudio * 1000
          : (remainingAudio / remainingPlies) * 1000;

        state.moveTimer = setTimeout(() => {
          if (!state.playing) return;
          advanceMove();
          scheduleResumeStep(idx + 1);
        }, Math.max(delayMs, 50));
      }

      scheduleResumeStep(0);
    } else {
      const intervalMs = Math.max((remainingAudio / remainingPlies) * 1000, 100);

      state.moveTimer = setInterval(() => {
        if (!state.playing) return;
        if (state.currentPly >= targetPly || state.currentPly >= state.totalPlies) {
          clearInterval(state.moveTimer);
          state.moveTimer = null;
          if (state.audioElement && !state.audioElement.ended) {
            state.audioElement.onended = () => { if (state.playing) playSegment(segIdx + 1); };
          } else {
            if (state.playing) playSegment(segIdx + 1);
          }
          return;
        }
        advanceMove();
      }, intervalMs);
    }

    // If audio ends before all moves, speed up remaining
    state.audioElement.onended = () => {
      if (!state.moveTimer) return;
      clearTimeout(state.moveTimer);
      state.moveTimer = setInterval(() => {
        if (!state.playing || state.currentPly >= targetPly || state.currentPly >= state.totalPlies) {
          clearInterval(state.moveTimer);
          state.moveTimer = null;
          if (state.playing) playSegment(segIdx + 1);
          return;
        }
        advanceMove();
      }, 400);
    };
  }

  function startPlayback() {
    if (state.currentPly >= state.totalPlies && state.segmentIdx >= state.control.segments.length) {
      // Game already finished, restart
      resetGamePosition();
    }

    state.playing = true;
    setPlayButton(true);
    updateFastForwardButton();

    // Resume from paused position if audio element is still valid
    if (state.audioElement && state.audioElement.paused && !state.audioElement.ended && state.audioElement.currentTime > 0) {
      resumeSegment();
    } else {
      playSegment(state.segmentIdx);
    }
  }

  function togglePlayback() {
    if (state.playing) {
      stopPlayback();
    } else {
      if (!state.game) return;
      startPlayback();
    }
  }

  function fastForward() {
    if (!state.playing || !state.control) return;
    const totalSegments = state.control.segments.length;
    if (state.segmentIdx >= totalSegments - 1) return;

    // Stop current audio and move timer
    if (state.moveTimer) {
      clearInterval(state.moveTimer);
      state.moveTimer = null;
    }
    if (state.audioElement) {
      state.audioElement.onended = null;
      state.audioElement.pause();
      state.audioElement = null;
    }

    // Fast-forward moves to the end of the current segment
    const seg = state.control.segments[state.segmentIdx];
    const plies = pliesInSegment(seg);
    const startPly = segmentStartPly(seg);
    const targetPly = startPly + plies;

    while (state.currentPly < targetPly && state.currentPly < state.totalPlies) {
      const fm = state.flatMoves[state.currentPly];
      const result = state.chess.move(fm.san, { sloppy: true });
      if (result) {
        state.board.position(state.chess.fen(), false);
      }
      state.currentPly++;
    }

    // Update UI to reflect skipped position
    if (state.currentPly > 0) {
      highlightPly(state.currentPly - 1);
    }

    // Play next segment
    playSegment(state.segmentIdx + 1);
  }

  function resetGamePosition() {
    stopPlayback();
    state.audioElement = null;
    state.chess = new Chess();
    state.board.position('start');
    state.currentPly = 0;
    state.segmentIdx = 0;
    dom.progressBar.style.width = '0%';
    dom.moveCounter.textContent = `Move: 0/${state.totalPlies}`;

    // Clear move highlights
    dom.moveList.querySelectorAll('.played, .active').forEach(el => {
      el.classList.remove('played', 'active');
    });

    setNarrative('Deep Red ready. Press \u25B6 to begin narration...');
  }

  // ===== Game Loading =====

  async function loadGame(entry) {
    showLoading();
    stopPlayback();
    state.audioElement = null;

    const gameId = entry.game_id;
    const basePath = `${DATA_BASE}/games/${gameId}`;

    try {
      const [gameData, controlData] = await Promise.all([
        fetchJSON(`${basePath}/game.json`),
        fetchJSON(`${basePath}/control.json`),
      ]);

      state.game = gameData;
      state.control = controlData;
      state.flatMoves = flattenMoves(gameData.moves);
      state.totalPlies = state.flatMoves.length;

      // Init chess.js
      state.chess = new Chess();

      // Init or update board
      if (!state.board) {
        state.board = Chessboard('board', {
          position: 'start',
          pieceTheme: 'img/chesspieces/wikipedia/{piece}.png',
          animationDuration: 300,
        });
      } else {
        state.board.position('start');
      }

      state.currentPly = 0;
      state.segmentIdx = 0;

      // Render UI
      renderGameInfo();
      renderMoveList();
      updateTotalDuration();
      updateSegmentDuration(null);
      dom.progressBar.style.width = '0%';
      dom.moveCounter.textContent = `Move: 0/${state.totalPlies}`;
      setNarrative('Deep Red ready. Press \u25B6 to begin narration...');

      hideLoading();
    } catch (err) {
      console.error('Failed to load game:', err);
      hideLoading();
      setNarrative('Deep Red could not load this game. Try another.');
    }
  }

  function buildShuffledPlaylist() {
    const indices = Array.from({ length: state.index.length }, (_, i) => i);
    shuffleArray(indices);
    state.shuffledPlaylist = indices;
    state.playlistPos = -1;
  }

  async function loadNextGame() {
    cancelAutoContinue();
    if (!state.index || state.index.length === 0) {
      setNarrative('No games in Deep Red\'s database.');
      return;
    }

    // If playlist exhausted, reshuffle
    if (state.playlistPos >= state.shuffledPlaylist.length - 1) {
      buildShuffledPlaylist();
    }

    state.playlistPos++;
    const entry = state.index[state.shuffledPlaylist[state.playlistPos]];
    updateGameCounter();
    await loadGame(entry);

    // Auto-start playback if auto-continue is on
    if (dom.chkAutoContinue.checked) {
      startPlayback();
    }
  }

  async function loadRandomGame() {
    cancelAutoContinue();
    if (!state.index || state.index.length === 0) {
      setNarrative('No games in Deep Red\'s database.');
      return;
    }

    // On manual "New Game", advance to next in playlist
    await loadNextGame();
  }

  // ===== Initialization =====

  async function init() {
    showLoading();

    try {
      state.index = await fetchJSON(`${DATA_BASE}/index.json`);
      console.log(`Loaded index: ${state.index.length} games`);
    } catch (err) {
      console.error('Failed to load game index:', err);
      hideLoading();
      setNarrative('Deep Red encountered an error loading the game index.');
      return;
    }

    // Event listeners
    dom.btnPlay.addEventListener('click', togglePlayback);
    dom.btnFastForward.addEventListener('click', fastForward);
    dom.btnNewGame.addEventListener('click', loadRandomGame);
    // Handle window resize for board
    window.addEventListener('resize', () => {
      if (state.board) state.board.resize();
    });

    // Build shuffled playlist and load first game
    buildShuffledPlaylist();
    await loadNextGame();
  }

  // Start when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

'use strict';

// ===== 설정 =====
const COLS = 10;
const ROWS = 20;
const BLOCK = 30;
const NEXT_BLOCK = 24;
const BEST_KEY = 'tetris-best-score';

const COLORS = {
  I: '#22d3ee',
  O: '#facc15',
  T: '#a855f7',
  S: '#22c55e',
  Z: '#ef4444',
  J: '#3b82f6',
  L: '#f97316',
};

const SHAPES = {
  I: [
    [0, 0, 0, 0],
    [1, 1, 1, 1],
    [0, 0, 0, 0],
    [0, 0, 0, 0],
  ],
  O: [
    [1, 1],
    [1, 1],
  ],
  T: [
    [0, 1, 0],
    [1, 1, 1],
    [0, 0, 0],
  ],
  S: [
    [0, 1, 1],
    [1, 1, 0],
    [0, 0, 0],
  ],
  Z: [
    [1, 1, 0],
    [0, 1, 1],
    [0, 0, 0],
  ],
  J: [
    [1, 0, 0],
    [1, 1, 1],
    [0, 0, 0],
  ],
  L: [
    [0, 0, 1],
    [1, 1, 1],
    [0, 0, 0],
  ],
};

// 한 번에 지운 줄 수에 따른 기본 점수 (레벨을 곱함)
const LINE_SCORES = [0, 100, 300, 500, 800];

// ===== DOM =====
const boardCanvas = document.getElementById('board');
const ctx = boardCanvas.getContext('2d');
const nextCanvas = document.getElementById('next');
const nextCtx = nextCanvas.getContext('2d');

const scoreEl = document.getElementById('score');
const linesEl = document.getElementById('lines');
const levelEl = document.getElementById('level');
const bestEl = document.getElementById('best');

const overlay = document.getElementById('overlay');
const overlayTitle = document.getElementById('overlay-title');
const overlayText = document.getElementById('overlay-text');
const startBtn = document.getElementById('start-btn');
const pauseBtn = document.getElementById('pause-btn');

// ===== 상태 =====
let board = createBoard();
let bag = [];
let piece = null;
let nextType = null;
let score = 0;
let lines = 0;
let level = 1;
let best = loadBest();
let dropInterval = 1000;
let dropCounter = 0;
let lastTime = 0;
let animationId = null;
let state = 'ready'; // ready | playing | paused | over

// ===== 유틸 =====
function createBoard() {
  return Array.from({ length: ROWS }, () => Array(COLS).fill(null));
}

function loadBest() {
  try {
    return Number(localStorage.getItem(BEST_KEY)) || 0;
  } catch (e) {
    return 0;
  }
}

function saveBest(value) {
  try {
    localStorage.setItem(BEST_KEY, String(value));
  } catch (e) {
    // localStorage 사용 불가 환경은 무시
  }
}

function refillBag() {
  const types = Object.keys(SHAPES);
  for (let i = types.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [types[i], types[j]] = [types[j], types[i]];
  }
  bag.push(...types);
}

function takeFromBag() {
  if (bag.length === 0) refillBag();
  return bag.shift();
}

function createPiece(type) {
  const matrix = SHAPES[type].map((row) => row.slice());
  return {
    type,
    matrix,
    x: Math.floor((COLS - matrix[0].length) / 2),
    y: type === 'I' ? -1 : 0,
  };
}

function collide(matrix, offsetX, offsetY) {
  for (let r = 0; r < matrix.length; r++) {
    for (let c = 0; c < matrix[r].length; c++) {
      if (!matrix[r][c]) continue;
      const x = offsetX + c;
      const y = offsetY + r;
      if (x < 0 || x >= COLS || y >= ROWS) return true;
      if (y >= 0 && board[y][x]) return true;
    }
  }
  return false;
}

function rotateMatrix(matrix, dir) {
  const n = matrix.length;
  const result = Array.from({ length: n }, () => Array(n).fill(0));
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (dir > 0) {
        result[c][n - 1 - r] = matrix[r][c]; // 시계 방향
      } else {
        result[n - 1 - c][r] = matrix[r][c]; // 반시계 방향
      }
    }
  }
  return result;
}

// ===== 게임 동작 =====
function spawn() {
  piece = createPiece(nextType);
  nextType = takeFromBag();
  if (collide(piece.matrix, piece.x, piece.y)) {
    gameOver();
    return false;
  }
  return true;
}

function move(dx) {
  if (!collide(piece.matrix, piece.x + dx, piece.y)) {
    piece.x += dx;
  }
}

function rotate(dir) {
  if (piece.type === 'O') return;
  const rotated = rotateMatrix(piece.matrix, dir);
  const kicks = [0, -1, 1, -2, 2];
  for (const k of kicks) {
    if (!collide(rotated, piece.x + k, piece.y)) {
      piece.matrix = rotated;
      piece.x += k;
      return;
    }
  }
  // 바닥에 닿아서 회전이 막힌 경우 한 칸 위로 올려서 시도
  if (!collide(rotated, piece.x, piece.y - 1)) {
    piece.matrix = rotated;
    piece.y -= 1;
  }
}

function softDrop() {
  if (!collide(piece.matrix, piece.x, piece.y + 1)) {
    piece.y += 1;
    score += 1;
    updateHUD();
  } else {
    lockPiece();
  }
  dropCounter = 0;
}

function gravityDrop() {
  if (!collide(piece.matrix, piece.x, piece.y + 1)) {
    piece.y += 1;
  } else {
    lockPiece();
  }
  dropCounter = 0;
}

function hardDrop() {
  let distance = 0;
  while (!collide(piece.matrix, piece.x, piece.y + 1)) {
    piece.y += 1;
    distance += 1;
  }
  score += distance * 2;
  lockPiece();
  dropCounter = 0;
}

function lockPiece() {
  let overflow = false;
  piece.matrix.forEach((row, r) => {
    row.forEach((value, c) => {
      if (!value) return;
      const y = piece.y + r;
      const x = piece.x + c;
      if (y < 0) {
        overflow = true;
      } else {
        board[y][x] = piece.type;
      }
    });
  });

  if (overflow) {
    gameOver();
    return;
  }

  clearLines();
  updateHUD();
  spawn();
}

function clearLines() {
  let cleared = 0;
  for (let r = ROWS - 1; r >= 0; r--) {
    if (board[r].every((cell) => cell !== null)) {
      board.splice(r, 1);
      board.unshift(Array(COLS).fill(null));
      cleared += 1;
      r += 1; // 당겨 내려온 줄을 다시 검사
    }
  }
  if (cleared > 0) {
    score += LINE_SCORES[cleared] * level;
    lines += cleared;
    level = Math.floor(lines / 10) + 1;
    dropInterval = Math.max(100, 1000 - (level - 1) * 90);
  }
}

function getGhostY() {
  let y = piece.y;
  while (!collide(piece.matrix, piece.x, y + 1)) {
    y += 1;
  }
  return y;
}

// ===== 그리기 =====
function drawCell(context, x, y, color, size, alpha = 1) {
  context.globalAlpha = alpha;
  context.fillStyle = color;
  context.fillRect(x * size + 1, y * size + 1, size - 2, size - 2);
  context.fillStyle = 'rgba(255, 255, 255, 0.18)';
  context.fillRect(x * size + 1, y * size + 1, size - 2, 4);
  context.globalAlpha = 1;
}

function drawGrid() {
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.08)';
  ctx.lineWidth = 1;
  for (let c = 1; c < COLS; c++) {
    ctx.beginPath();
    ctx.moveTo(c * BLOCK + 0.5, 0);
    ctx.lineTo(c * BLOCK + 0.5, ROWS * BLOCK);
    ctx.stroke();
  }
  for (let r = 1; r < ROWS; r++) {
    ctx.beginPath();
    ctx.moveTo(0, r * BLOCK + 0.5);
    ctx.lineTo(COLS * BLOCK, r * BLOCK + 0.5);
    ctx.stroke();
  }
}

function drawMatrix(matrix, offsetX, offsetY, color, alpha) {
  matrix.forEach((row, r) => {
    row.forEach((value, c) => {
      if (!value) return;
      const y = offsetY + r;
      if (y < 0) return;
      drawCell(ctx, offsetX + c, y, color, BLOCK, alpha);
    });
  });
}

function draw() {
  ctx.clearRect(0, 0, boardCanvas.width, boardCanvas.height);
  drawGrid();

  board.forEach((row, r) => {
    row.forEach((type, c) => {
      if (type) drawCell(ctx, c, r, COLORS[type], BLOCK);
    });
  });

  if (piece && state !== 'over') {
    const color = COLORS[piece.type];
    drawMatrix(piece.matrix, piece.x, getGhostY(), color, 0.22);
    drawMatrix(piece.matrix, piece.x, piece.y, color, 1);
  }

  drawNext();
}

function drawNext() {
  nextCtx.clearRect(0, 0, nextCanvas.width, nextCanvas.height);
  if (!nextType) return;

  const matrix = SHAPES[nextType];
  let minR = Infinity;
  let maxR = -1;
  let minC = Infinity;
  let maxC = -1;
  matrix.forEach((row, r) => {
    row.forEach((value, c) => {
      if (!value) return;
      minR = Math.min(minR, r);
      maxR = Math.max(maxR, r);
      minC = Math.min(minC, c);
      maxC = Math.max(maxC, c);
    });
  });

  const w = (maxC - minC + 1) * NEXT_BLOCK;
  const h = (maxR - minR + 1) * NEXT_BLOCK;
  const ox = (nextCanvas.width - w) / 2;
  const oy = (nextCanvas.height - h) / 2;

  nextCtx.save();
  nextCtx.translate(ox, oy);
  matrix.forEach((row, r) => {
    row.forEach((value, c) => {
      if (value) drawCell(nextCtx, c - minC, r - minR, COLORS[nextType], NEXT_BLOCK);
    });
  });
  nextCtx.restore();
}

function updateHUD() {
  scoreEl.textContent = score.toLocaleString();
  linesEl.textContent = lines;
  levelEl.textContent = level;
  bestEl.textContent = Math.max(best, score).toLocaleString();
}

// ===== 루프 / 상태 전환 =====
function loop(time) {
  if (state !== 'playing') return;
  const delta = time - lastTime;
  lastTime = time;
  dropCounter += delta;
  if (dropCounter > dropInterval) {
    gravityDrop();
  }
  draw();
  if (state === 'playing') {
    animationId = requestAnimationFrame(loop);
  }
}

function showOverlay(title, text, buttonLabel) {
  overlayTitle.textContent = title;
  overlayText.textContent = text;
  startBtn.textContent = buttonLabel;
  overlay.classList.remove('hidden');
}

function hideOverlay() {
  overlay.classList.add('hidden');
}

function startGame() {
  board = createBoard();
  bag = [];
  score = 0;
  lines = 0;
  level = 1;
  dropInterval = 1000;
  dropCounter = 0;
  nextType = takeFromBag();
  state = 'playing';
  pauseBtn.textContent = '일시정지';
  hideOverlay();
  updateHUD();
  if (!spawn()) return;
  cancelAnimationFrame(animationId);
  lastTime = performance.now();
  animationId = requestAnimationFrame(loop);
}

function togglePause() {
  if (state === 'playing') {
    state = 'paused';
    cancelAnimationFrame(animationId);
    pauseBtn.textContent = '계속하기';
    showOverlay('일시정지', 'P 또는 Esc 키로 계속할 수 있습니다', '계속하기');
  } else if (state === 'paused') {
    state = 'playing';
    pauseBtn.textContent = '일시정지';
    hideOverlay();
    lastTime = performance.now();
    animationId = requestAnimationFrame(loop);
  }
}

function gameOver() {
  state = 'over';
  cancelAnimationFrame(animationId);
  if (score > best) {
    best = score;
    saveBest(best);
  }
  updateHUD();
  draw();
  showOverlay(
    'GAME OVER',
    `점수: ${score.toLocaleString()}\n라인: ${lines} / 레벨: ${level}`,
    '다시 하기'
  );
}

// ===== 입력 =====
function handleAction(action) {
  if (state !== 'playing') return;
  switch (action) {
    case 'left':
      move(-1);
      break;
    case 'right':
      move(1);
      break;
    case 'down':
      softDrop();
      break;
    case 'rotate':
      rotate(1);
      break;
    case 'rotateCCW':
      rotate(-1);
      break;
    case 'drop':
      hardDrop();
      break;
    default:
      return;
  }
  if (state === 'playing') draw();
}

const KEY_ACTIONS = {
  ArrowLeft: 'left',
  ArrowRight: 'right',
  ArrowDown: 'down',
  ArrowUp: 'rotate',
  KeyX: 'rotate',
  KeyZ: 'rotateCCW',
  Space: 'drop',
};

document.addEventListener('keydown', (event) => {
  if (event.code === 'Enter' && (state === 'ready' || state === 'over')) {
    event.preventDefault();
    startGame();
    return;
  }

  if (event.code === 'KeyP' || event.code === 'Escape') {
    event.preventDefault();
    togglePause();
    return;
  }

  const action = KEY_ACTIONS[event.code];
  if (!action) return;
  event.preventDefault();
  if (event.code === 'Space' && event.repeat) return;
  handleAction(action);
});

startBtn.addEventListener('click', () => {
  startBtn.blur();
  if (state === 'paused') {
    togglePause();
  } else {
    startGame();
  }
});

pauseBtn.addEventListener('click', () => {
  pauseBtn.blur();
  togglePause();
});

document.querySelectorAll('.touch-controls [data-action]').forEach((button) => {
  button.addEventListener('click', () => {
    button.blur();
    handleAction(button.dataset.action);
  });
});

// ===== 초기 화면 =====
updateHUD();
draw();

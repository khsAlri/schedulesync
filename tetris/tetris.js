'use strict';

const COLS = 10;
const ROWS = 20;
const BLOCK = 30;

const COLORS = {
  I: '#00bcd4',
  J: '#3f51b5',
  L: '#ff9800',
  O: '#ffeb3b',
  S: '#4caf50',
  T: '#9c27b0',
  Z: '#f44336',
};

const SHAPES = {
  I: [[0, 0, 0, 0], [1, 1, 1, 1], [0, 0, 0, 0], [0, 0, 0, 0]],
  J: [[1, 0, 0], [1, 1, 1], [0, 0, 0]],
  L: [[0, 0, 1], [1, 1, 1], [0, 0, 0]],
  O: [[1, 1], [1, 1]],
  S: [[0, 1, 1], [1, 1, 0], [0, 0, 0]],
  T: [[0, 1, 0], [1, 1, 1], [0, 0, 0]],
  Z: [[1, 1, 0], [0, 1, 1], [0, 0, 0]],
};

const LINE_SCORES = [0, 100, 300, 500, 800];

const boardCanvas = document.getElementById('board');
const ctx = boardCanvas.getContext('2d');
const nextCanvas = document.getElementById('next');
const nextCtx = nextCanvas.getContext('2d');
const scoreEl = document.getElementById('score');
const linesEl = document.getElementById('lines');
const levelEl = document.getElementById('level');
const startBtn = document.getElementById('start');

let board = [];
let current = null;
let next = null;
let bag = [];
let score = 0;
let lines = 0;
let level = 1;
let dropInterval = 1000;
let dropCounter = 0;
let lastTime = 0;
let running = false;
let paused = false;
let gameOver = false;

function createBoard() {
  return Array.from({ length: ROWS }, () => Array(COLS).fill(null));
}

// 7개 블록을 섞어 순서대로 꺼내는 방식 (같은 블록이 너무 몰리지 않게)
function refillBag() {
  bag = Object.keys(SHAPES);
  for (let i = bag.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [bag[i], bag[j]] = [bag[j], bag[i]];
  }
}

function takeFromBag() {
  if (bag.length === 0) {
    refillBag();
  }
  return bag.pop();
}

function createPiece(type) {
  const matrix = SHAPES[type].map((row) => row.slice());
  return {
    type,
    matrix,
    x: Math.floor((COLS - matrix[0].length) / 2),
    y: 0,
  };
}

function collides(matrix, offsetX, offsetY) {
  for (let y = 0; y < matrix.length; y++) {
    for (let x = 0; x < matrix[y].length; x++) {
      if (!matrix[y][x]) continue;
      const bx = offsetX + x;
      const by = offsetY + y;
      if (bx < 0 || bx >= COLS || by >= ROWS) return true;
      if (by >= 0 && board[by][bx]) return true;
    }
  }
  return false;
}

// 시계 방향 90도 회전
function rotate(matrix) {
  const n = matrix.length;
  return matrix.map((row, y) => row.map((_, x) => matrix[n - 1 - x][y]));
}

function rotatePiece() {
  const rotated = rotate(current.matrix);
  // 벽에 막히면 좌우로 조금씩 밀어서 회전 시도
  const kicks = [0, -1, 1, -2, 2];
  for (const kick of kicks) {
    if (!collides(rotated, current.x + kick, current.y)) {
      current.matrix = rotated;
      current.x += kick;
      return;
    }
  }
}

function move(dir) {
  if (!collides(current.matrix, current.x + dir, current.y)) {
    current.x += dir;
  }
}

function step() {
  if (!collides(current.matrix, current.x, current.y + 1)) {
    current.y++;
  } else {
    lockPiece();
  }
  dropCounter = 0;
}

function softDrop() {
  if (!collides(current.matrix, current.x, current.y + 1)) {
    current.y++;
    score += 1;
    updateStats();
  } else {
    lockPiece();
  }
  dropCounter = 0;
}

function hardDrop() {
  let distance = 0;
  while (!collides(current.matrix, current.x, current.y + 1)) {
    current.y++;
    distance++;
  }
  score += distance * 2;
  updateStats();
  lockPiece();
  dropCounter = 0;
}

function ghostY() {
  let y = current.y;
  while (!collides(current.matrix, current.x, y + 1)) {
    y++;
  }
  return y;
}

function lockPiece() {
  current.matrix.forEach((row, y) => {
    row.forEach((value, x) => {
      if (!value) return;
      const by = current.y + y;
      if (by >= 0) {
        board[by][current.x + x] = current.type;
      }
    });
  });
  clearLines();
  spawn();
}

function clearLines() {
  let cleared = 0;
  for (let y = ROWS - 1; y >= 0; y--) {
    if (board[y].every((cell) => cell)) {
      board.splice(y, 1);
      board.unshift(Array(COLS).fill(null));
      cleared++;
      y++;
    }
  }
  if (cleared > 0) {
    lines += cleared;
    score += LINE_SCORES[cleared] * level;
    level = Math.floor(lines / 10) + 1;
    dropInterval = Math.max(100, 1000 - (level - 1) * 100);
    updateStats();
  }
}

function spawn() {
  current = createPiece(next);
  next = takeFromBag();
  drawNext();
  if (collides(current.matrix, current.x, current.y)) {
    endGame();
  }
}

function endGame() {
  running = false;
  gameOver = true;
  startBtn.textContent = '다시 시작';
}

function updateStats() {
  scoreEl.textContent = score;
  linesEl.textContent = lines;
  levelEl.textContent = level;
}

function drawCell(context, x, y, color, size, alpha = 1) {
  context.globalAlpha = alpha;
  context.fillStyle = color;
  context.fillRect(x * size, y * size, size, size);
  context.strokeStyle = 'rgba(0, 0, 0, 0.35)';
  context.strokeRect(x * size + 0.5, y * size + 0.5, size - 1, size - 1);
  context.globalAlpha = 1;
}

function drawMatrix(context, matrix, offsetX, offsetY, color, size, alpha) {
  matrix.forEach((row, y) => {
    row.forEach((value, x) => {
      if (value && offsetY + y >= 0) {
        drawCell(context, offsetX + x, offsetY + y, color, size, alpha);
      }
    });
  });
}

function drawOverlay(text) {
  ctx.fillStyle = 'rgba(0, 0, 0, 0.6)';
  ctx.fillRect(0, 0, boardCanvas.width, boardCanvas.height);
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 24px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, boardCanvas.width / 2, boardCanvas.height / 2);
}

function draw() {
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, boardCanvas.width, boardCanvas.height);

  // 배경 격자
  ctx.strokeStyle = '#1e1e1e';
  for (let x = 0; x <= COLS; x++) {
    ctx.beginPath();
    ctx.moveTo(x * BLOCK + 0.5, 0);
    ctx.lineTo(x * BLOCK + 0.5, ROWS * BLOCK);
    ctx.stroke();
  }
  for (let y = 0; y <= ROWS; y++) {
    ctx.beginPath();
    ctx.moveTo(0, y * BLOCK + 0.5);
    ctx.lineTo(COLS * BLOCK, y * BLOCK + 0.5);
    ctx.stroke();
  }

  board.forEach((row, y) => {
    row.forEach((type, x) => {
      if (type) {
        drawCell(ctx, x, y, COLORS[type], BLOCK);
      }
    });
  });

  if (current && !gameOver) {
    drawMatrix(ctx, current.matrix, current.x, ghostY(), COLORS[current.type], BLOCK, 0.25);
    drawMatrix(ctx, current.matrix, current.x, current.y, COLORS[current.type], BLOCK, 1);
  }

  if (gameOver) {
    drawOverlay('게임 오버');
  } else if (paused) {
    drawOverlay('일시정지');
  }
}

function drawNext() {
  nextCtx.fillStyle = '#111';
  nextCtx.fillRect(0, 0, nextCanvas.width, nextCanvas.height);
  if (!next) return;

  const size = 24;
  const cells = [];
  SHAPES[next].forEach((row, y) => {
    row.forEach((value, x) => {
      if (value) cells.push([x, y]);
    });
  });

  const xs = cells.map((c) => c[0]);
  const ys = cells.map((c) => c[1]);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const width = Math.max(...xs) - minX + 1;
  const height = Math.max(...ys) - minY + 1;
  const offsetX = (nextCanvas.width / size - width) / 2 - minX;
  const offsetY = (nextCanvas.height / size - height) / 2 - minY;

  cells.forEach(([x, y]) => {
    drawCell(nextCtx, x + offsetX, y + offsetY, COLORS[next], size);
  });
}

function update(time) {
  const delta = time - lastTime;
  lastTime = time;

  if (!paused) {
    dropCounter += delta;
    if (dropCounter > dropInterval) {
      step();
    }
  }

  draw();

  if (running) {
    requestAnimationFrame(update);
  }
}

function startGame() {
  board = createBoard();
  bag = [];
  score = 0;
  lines = 0;
  level = 1;
  dropInterval = 1000;
  dropCounter = 0;
  paused = false;
  gameOver = false;

  next = takeFromBag();
  spawn();
  updateStats();

  startBtn.textContent = '다시 시작';
  // 버튼에 포커스가 남아 있으면 Space 키가 버튼을 다시 누르므로 해제
  startBtn.blur();

  if (!running) {
    running = true;
    lastTime = performance.now();
    requestAnimationFrame(update);
  }
}

document.addEventListener('keydown', (event) => {
  if (!running || gameOver || !current) return;

  if (event.code === 'KeyP') {
    paused = !paused;
    return;
  }
  if (paused) return;

  switch (event.code) {
    case 'ArrowLeft':
      move(-1);
      break;
    case 'ArrowRight':
      move(1);
      break;
    case 'ArrowDown':
      softDrop();
      break;
    case 'ArrowUp':
      rotatePiece();
      break;
    case 'Space':
      hardDrop();
      break;
    default:
      return;
  }
  event.preventDefault();
});

startBtn.addEventListener('click', startGame);

// 초기 화면
board = createBoard();
draw();
drawOverlay('시작 버튼을 누르세요');
drawNext();

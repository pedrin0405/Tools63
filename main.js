const { app, BrowserWindow, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let mainWindow;
let pythonProcess = null;

function pipeBackendOutput(stream, kind = 'stdout') {
  let pending = '';
  const accessLogRe = /"\s(\d{3})\s/;
  const severeRe = /(Traceback|ERROR in app|Exception on|\bCRITICAL\b|\bFATAL\b)/i;

  stream.on('data', (chunk) => {
    pending += chunk.toString('utf8');
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() || '';

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      if (!line.trim()) continue;

      // Flask writes request access logs to stderr by default; avoid labeling them as errors.
      if (kind === 'stderr') {
        const match = line.match(accessLogRe);
        if (match) {
          const status = Number(match[1]);
          if (status >= 500) console.error(`Backend Error: ${line}`);
          else if (status >= 400) console.warn(`Backend Warn: ${line}`);
          else console.log(`Backend: ${line}`);
          continue;
        }

        if (severeRe.test(line)) {
          console.error(`Backend Error: ${line}`);
        } else {
          console.warn(`Backend Warn: ${line}`);
        }
        continue;
      }

      console.log(`Backend: ${line}`);
    }
  });

  stream.on('end', () => {
    const line = pending.trim();
    if (!line) return;
    if (kind === 'stderr') console.warn(`Backend Warn: ${line}`);
    else console.log(`Backend: ${line}`);
    pending = '';
  });
}

function createPythonBackend() {
  const isWin = process.platform === 'win32';
  
  if (app.isPackaged) {
    // Em produção, rodamos o executável gerado pelo PyInstaller
    const binaryName = isWin ? 'server.exe' : 'server';
    // O electron-builder coloca os arquivos em 'Resources' (mac) ou 'resources' (win)
    const binaryPath = path.join(process.resourcesPath, 'backend', 'dist', binaryName);
    
    pythonProcess = spawn(binaryPath, [], {
      cwd: process.resourcesPath
    });
  } else {
    // Em desenvolvimento, rodamos via script .py usando o venv local
    const scriptPath = path.join(__dirname, 'backend', 'server.py');
    const pythonExe = isWin 
      ? path.join(__dirname, '.venv', 'Scripts', 'python.exe')
      : path.join(__dirname, '.venv', 'bin', 'python');
    
    pythonProcess = spawn(pythonExe, [scriptPath], {
      cwd: __dirname
    });
  }

  pipeBackendOutput(pythonProcess.stdout, 'stdout');
  pipeBackendOutput(pythonProcess.stderr, 'stderr');

  pythonProcess.on('close', (code) => {
    console.log(`Backend process exited with code ${code}`);
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 850,
    fullscreen: true,
    titleBarStyle: 'hiddenInset', // Apple style traffic lights
    backgroundColor: '#0c0a1a',
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'src', 'index.html'));

  mainWindow.on('closed', function () {
    mainWindow = null;
  });
}

app.on('ready', () => {
  createPythonBackend();
  createWindow();
});

app.on('window-all-closed', function () {
  if (pythonProcess) {
    pythonProcess.kill();
  }
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', function () {
  if (mainWindow === null) createWindow();
});

app.on('quit', () => {
  if (pythonProcess) {
    pythonProcess.kill();
  }
});

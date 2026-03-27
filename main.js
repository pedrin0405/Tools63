const { app, BrowserWindow, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let mainWindow;
let pythonProcess = null;

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

  pythonProcess.stdout.on('data', (data) => {
    console.log(`Backend: ${data}`);
  });

  pythonProcess.stderr.on('data', (data) => {
    console.error(`Backend Error: ${data}`);
  });

  pythonProcess.on('close', (code) => {
    console.log(`Backend process exited with code ${code}`);
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 850,
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

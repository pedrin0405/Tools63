const { app, BrowserWindow, shell, ipcMain } = require('electron');
const path = require('path');
const { spawn, execSync } = require('child_process');

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

// ─── Git Auto-Update System ────────────────────────────────────────────
function runGit(args) {
  try {
    return execSync(`git ${args}`, { cwd: __dirname, encoding: 'utf8', timeout: 30000 }).trim();
  } catch (e) {
    console.error(`Git error (${args}):`, e.message);
    return null;
  }
}

function isGitRepo() {
  try {
    execSync('git rev-parse --is-inside-work-tree', { cwd: __dirname, encoding: 'utf8' });
    return true;
  } catch { return false; }
}

// IPC: Verifica se há atualizações disponíveis
ipcMain.handle('check-for-updates', async () => {
  if (!isGitRepo()) return { available: false, error: 'not_git', message: 'Projeto não é um repositório Git' };
  
  try {
    // Fetch latest from remote without merging
    runGit('fetch origin');
    
    const localHash = runGit('rev-parse HEAD');
    const remoteHash = runGit('rev-parse origin/main') || runGit('rev-parse origin/master');
    
    if (!localHash || !remoteHash) return { available: false, error: 'no_remote', message: 'Não foi possível verificar o remoto' };
    if (localHash === remoteHash) return { available: false, currentVersion: localHash.substring(0, 7) };
    
    // Get commits between local and remote
    const logOutput = runGit('log --oneline HEAD..origin/main') || runGit('log --oneline HEAD..origin/master') || '';
    const commits = logOutput.split('\n').filter(l => l.trim());
    
    // Get the date of the latest remote commit
    const latestDate = runGit('log -1 --format=%ci origin/main') || runGit('log -1 --format=%ci origin/master') || '';
    
    return {
      available: true,
      currentVersion: localHash.substring(0, 7),
      remoteVersion: remoteHash.substring(0, 7),
      commitCount: commits.length,
      commits: commits.slice(0, 10), // Últimos 10 commits
      latestDate: latestDate
    };
  } catch (e) {
    return { available: false, error: 'fetch_failed', message: e.message };
  }
});

// IPC: Aplica a atualização (git pull + install deps se necessário)
ipcMain.handle('apply-update', async (event) => {
  if (!isGitRepo()) return { success: false, error: 'Não é um repositório Git' };
  
  try {
    // Salvar hashes dos arquivos de dependência antes do pull
    const pkgBefore = runGit('rev-parse HEAD:package.json') || '';
    const reqBefore = runGit('rev-parse HEAD:backend/requirements.txt') || '';
    
    // Notificar progresso
    if (mainWindow) mainWindow.webContents.send('update-progress', { stage: 'pull', message: 'Baixando atualizações...' });
    
    // Stash local changes (para não perder trabalho do usuário)
    const stashResult = runGit('stash');
    const hadStash = stashResult && !stashResult.includes('No local changes');
    
    // Pull
    const pullResult = runGit('pull --ff-only origin main') || runGit('pull --ff-only origin master');
    if (!pullResult) {
      // Se ff-only falhar, tentar rebase
      const rebaseResult = runGit('pull --rebase origin main') || runGit('pull --rebase origin master');
      if (!rebaseResult) {
        if (hadStash) runGit('stash pop');
        return { success: false, error: 'Pull falhou. Possível conflito de merge.' };
      }
    }
    
    // Restaurar stash
    if (hadStash) runGit('stash pop');
    
    // Checar se dependências mudaram
    const pkgAfter = runGit('rev-parse HEAD:package.json') || '';
    const reqAfter = runGit('rev-parse HEAD:backend/requirements.txt') || '';
    
    let depsUpdated = false;
    
    // npm install se package.json mudou
    if (pkgBefore !== pkgAfter) {
      if (mainWindow) mainWindow.webContents.send('update-progress', { stage: 'npm', message: 'Instalando dependências Node.js...' });
      try {
        execSync('npm install', { cwd: __dirname, encoding: 'utf8', timeout: 120000 });
        depsUpdated = true;
      } catch (e) {
        console.warn('npm install warning:', e.message);
      }
    }
    
    // pip install se requirements.txt mudou
    if (reqBefore !== reqAfter) {
      if (mainWindow) mainWindow.webContents.send('update-progress', { stage: 'pip', message: 'Instalando dependências Python...' });
      const isWin = process.platform === 'win32';
      const pipExe = isWin 
        ? path.join(__dirname, '.venv', 'Scripts', 'pip.exe')
        : path.join(__dirname, '.venv', 'bin', 'pip');
      const reqPath = path.join(__dirname, 'backend', 'requirements.txt');
      try {
        execSync(`"${pipExe}" install -r "${reqPath}"`, { cwd: __dirname, encoding: 'utf8', timeout: 120000 });
        depsUpdated = true;
      } catch (e) {
        console.warn('pip install warning:', e.message);
      }
    }
    
    const newHash = runGit('rev-parse HEAD');
    
    if (mainWindow) mainWindow.webContents.send('update-progress', { stage: 'done', message: 'Atualização concluída!' });
    
    return {
      success: true,
      newVersion: newHash ? newHash.substring(0, 7) : 'unknown',
      depsUpdated,
      pullResult
    };
  } catch (e) {
    return { success: false, error: e.message };
  }
});

// IPC: Reinicia o app
ipcMain.handle('restart-app', async () => {
  if (pythonProcess) pythonProcess.kill();
  app.relaunch();
  app.exit(0);
});

// IPC: Obtém versão atual
ipcMain.handle('get-app-version', async () => {
  const version = isGitRepo() ? (runGit('rev-parse --short HEAD') || 'dev') : 'packaged';
  const branch = isGitRepo() ? (runGit('rev-parse --abbrev-ref HEAD') || 'main') : '';
  return { version, branch, isGit: isGitRepo() };
});

// ─── Window & App Lifecycle ────────────────────────────────────────────
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


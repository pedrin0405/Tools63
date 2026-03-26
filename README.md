# 🛠️ Tools63 — Ultimate SaaS Desktop Suite

<div align="center">
  <img src="https://64.media.tumblr.com/76504a956163359d93370e30d4b89ea8/tumblr_mwm09l0v1F1sqovp5o1_500.gifv" alt="Tools63 Banner" width="600">
  <p><i>A suíte de ferramentas definitiva para produtores de conteúdo e usuários avançados.</i></p>
  
  [![GitHub Release](https://img.shields.io/github/v/release/pedrin0405/Tools63?include_prereleases&style=for-the-badge&color=7c3aed)](https://github.com/pedrin0405/Tools63/releases/latest)
  [![Build Status](https://img.shields.io/github/actions/workflow/status/pedrin0405/Tools63/build.yml?branch=main&style=for-the-badge)](https://github.com/pedrin0405/Tools63/actions)
  [![License](https://img.shields.io/badge/license-ISC-blue?style=for-the-badge)](LICENSE)
</div>

---

## ✨ Sobre o Tools63

O **Tools63** é um hub de ferramentas desktop de alta performance, projetado com uma estética **"Apple Glass"** e **Bento UI**. Ele combina a simplicidade de uma interface web moderna com a potência bruta de processamento local.

### 🎥 Módulo em Destaque: YouTube Downloader Pro
O primeiro módulo integrado à suíte permite baixar vídeos e playlists do YouTube com facilidade:
- **Resolução Máxima**: Baixe em até 4K/8K com um clique.
- **Várias Conversões**: MP4 (Vídeo) ou MP3 (Áudio em alta qualidade).
- **Processamento em Fila**: Adicione vários vídeos e deixe o Tools63 gerenciar o download em segundo plano.
- **Status em Tempo Real**: Veja velocidade de download, ETA e progresso detalhado.

---

## 📥 Downloads (Instalação Fácil)

Você não precisa de conhecimento técnico para usar o Tools63. Basta escolher seu sistema operacional no link oficial abaixo:

### 🔥 **[Baixar Tools63 para Windows & macOS](https://github.com/pedrin0405/Tools63/releases/latest)**

*   **Windows**: Baixe o arquivo `.exe` (Versão Portátil ou Instalador).
*   **macOS**: Baixe o arquivo `.dmg` e arraste para Aplicações.

---

## 🚀 Como Funciona a Automação?

O Tools63 usa um sistema de **CI/CD (Integração e Distribuição Contínua)** de ponta via GitHub Actions.

1.  **Desenvolvedor envia o código**: Sempre que uma melhoria é feita, o GitHub detecta automaticamente.
2.  **Build Multi-plataforma**: O código é compilado simultaneamente em servidores Mac e Windows.
3.  **Release Automática**: Os novos instaladores são gerados e ficam imediatamente disponíveis para o usuário final no link oficial.

---

## 🛠️ Stack Tecnológica

- **Frontend**: HTML5, Vanilla CSS (Glassmorphism), JavaScript.
- **Core (Shell)**: [Electron](https://www.electronjs.org/) para execução desktop fluida.
- **Backend (Engine)**: Python 3.12 com Flask (API de processamento).
- **Processamento de Mídia**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) e [FFmpeg](https://ffmpeg.org/).
- **Compilação**: [PyInstaller](https://pyinstaller.org/) para isolar o Python no executável.

---

## ⚡ Começando em Desenvolvimento

Se você é um desenvolvedor e quer rodar o projeto localmente:

1.  **Clone o repositório**:
    ```bash
    git clone https://github.com/pedrin0405/Tools63.git
    cd Tools63
    ```

2.  **Instale as dependências do Node (Electron)**:
    ```bash
    npm install
    ```

3.  **Configure o Backend Python**:
    ```bash
    cd backend
    python -m venv .venv
    source .venv/bin/activate  # ou .venv\Scripts\activate no Windows
    pip install -r requirements.txt
    ```

4.  **Inicie o App**:
    ```bash
    npm start
    ```

---

<div align="center">
  <p>Desenvolvido com ❤️ pela equipe Tools63.</p>
  <img src="https://readme-typing-svg.demolab.com?font=Fira+Code&pause=1000&color=7C3AED&center=true&vCenter=true&width=435&lines=Potência+Desktop;Experiência+Apple+Glass;Tools63+Pro" alt="Typing SVG" />
</div>

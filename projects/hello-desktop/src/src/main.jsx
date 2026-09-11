import React from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

function App() {
  return (
    <main>
      <p className="eyebrow">AI Gang desktop template</p>
      <h1>Hello from Tauri</h1>
      <p>
        This React UI runs in the browser for fast iteration and inside the native
        shell for Windows, macOS, and Linux.
      </p>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);

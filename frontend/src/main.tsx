import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './theme.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode><BrowserRouter><main aria-label="Watt Profile" /></BrowserRouter></StrictMode>,
)

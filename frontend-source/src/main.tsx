// antd v5 静态方法（message/notification）在 React 19 下需要此补丁，
// 否则 message.success 等调用静默失效（toast 不渲染）
import '@ant-design/v5-patch-for-react-19';
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { bootstrapBiTokenFromUrl } from './api/client'

// 投放链接的只读令牌必须在首个请求之前落入 localStorage：否则 BiGate 会先发
// 未鉴权的 /bi/status，投放页无法挂载（详见 client.ts 注释）
bootstrapBiTokenFromUrl();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

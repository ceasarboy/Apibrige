import React, { lazy, Suspense } from 'react';
import BiGate from './components/BiGate';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import MainLayout from './layouts/MainLayout';
import DashboardPage from './pages/dashboard';
import ConnectionsPage from './pages/connections';
import MappingsPage from './pages/mappings';
import FormsPage from './pages/forms';
import TasksPage from './pages/tasks';
import LogsPage from './pages/logs';
import TestingPage from './pages/testing';
import DebugPage from './pages/debug';
import ProbesPage from './pages/probes';

const BiPage = lazy(() => import('./pages/bi'));
const BiDefinitionsPage = lazy(() => import('./pages/bi/definitions'));
const BiScreenPage = lazy(() => import('./pages/bi/screen'));
const BiAnalysisPage = lazy(() => import('./pages/bi/analysis'));
const BiAccessPage = lazy(() => import('./pages/bi/access'));
const biView = (node: React.ReactNode) => <BiGate><Suspense fallback={<div>加载中…</div>}>{node}</Suspense></BiGate>;

const App: React.FC = () => {
  return (
    <ConfigProvider locale={zhCN}>
      <BrowserRouter>
        <Routes>
          <Route path="/bi/screen" element={biView(<BiScreenPage />)} />
          <Route path="/" element={<MainLayout />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="bi" element={biView(<BiPage />)} />
            <Route path="bi/definitions" element={biView(<BiDefinitionsPage />)} />
            <Route path="bi/analysis" element={biView(<BiAnalysisPage />)} />
            <Route path="bi/access" element={biView(<BiAccessPage />)} />
            <Route path="dashboard" element={<DashboardPage />} />
            <Route path="connections" element={<ConnectionsPage />} />
            <Route path="mappings" element={<MappingsPage />} />
            <Route path="forms" element={<FormsPage />} />
            <Route path="tasks" element={<TasksPage />} />
            <Route path="logs" element={<LogsPage />} />
            <Route path="testing" element={<TestingPage />} />
            <Route path="debug" element={<DebugPage />} />
            <Route path="probes" element={<ProbesPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  );
};

export default App;

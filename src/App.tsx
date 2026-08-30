import { useEffect } from 'react';
import Sidebar from './components/Sidebar';
import Toolbar from './components/Toolbar';
import GlobalAppendBar from './components/GlobalAppendBar';
import FilterBar from './components/FilterBar';
import FlowCanvas from './components/FlowCanvas';
import ModelConfigModal from './components/ModelConfigModal';
import UsageBar from './components/UsageBar';
import { initApp } from './store/useGraphStore';

export default function App() {
  useEffect(() => {
    initApp();
  }, []);

  return (
    <div className="app">
      <Sidebar />
      <div className="app-main">
        <Toolbar />
        <GlobalAppendBar />
        <FilterBar />
        <FlowCanvas />
        <UsageBar />
      </div>
      <ModelConfigModal />
    </div>
  );
}

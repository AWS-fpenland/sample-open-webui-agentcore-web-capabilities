// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Routes + sign-in gate. Mock mode bypasses the gate (a "mock mode" chip is shown in the shell).
import { Route, Routes, useLocation } from 'react-router-dom';
import { Shell } from './components/Shell';
import { ToastProvider } from './components/Toast';
import { ModelsProvider } from './lib/models';
import { AuthCallbackPage, LoadingPage, NotFoundPage, SignInPage, SignOutPage } from './pages/AuthPages';
import ComparePage from './pages/ComparePage';
import ExportsPage from './pages/ExportsPage';
import LibraryPage from './pages/LibraryPage';
import ModelLabPage from './pages/ModelLabPage';
import PackagePage from './pages/PackagePage';
import { useSession } from './session';

export default function App() {
  const session = useSession();
  const loc = useLocation();

  if (loc.pathname === '/auth/callback') return <AuthCallbackPage />;
  if (loc.pathname === '/signout') return <SignOutPage />;
  if (!session.mock) {
    if (session.loading) return <LoadingPage text="Checking your session…" />;
    if (!session.authenticated) return <SignInPage />;
  }

  return (
    <ToastProvider>
      <ModelsProvider>
        <Shell>
          <Routes>
            <Route path="/" element={<LibraryPage />} />
            <Route path="/p/:jobId" element={<PackagePage />} />
            <Route path="/compare/:a/:b" element={<ComparePage />} />
            <Route path="/lab" element={<ModelLabPage />} />
            <Route path="/exports/:jobId" element={<ExportsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Shell>
      </ModelsProvider>
    </ToastProvider>
  );
}

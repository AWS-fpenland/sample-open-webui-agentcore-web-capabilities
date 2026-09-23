// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Identity abstraction so pages never touch react-oidc-context directly.
import { createContext, useContext } from 'react';

export interface Session {
  authenticated: boolean;
  loading: boolean;
  error: string | null;
  email: string | null;
  signIn(returnTo?: string): void;
  signOut(): void;
}

export const POST_LOGIN_KEY = 'aiq-wb-postLoginRoute';
export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const s = useContext(SessionContext);
  if (!s) throw new Error('SessionContext missing');
  return s;
}

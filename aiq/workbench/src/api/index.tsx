// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { createContext, useContext } from 'react';
import type { WorkbenchApi } from './client';

export const ApiContext = createContext<WorkbenchApi | null>(null);

export function useApi(): WorkbenchApi {
  const api = useContext(ApiContext);
  if (!api) throw new Error('ApiContext missing');
  return api;
}

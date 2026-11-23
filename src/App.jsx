import { useState, useEffect, useCallback, useRef } from 'react';

// 1. Red Hat PatternFly Enterprise Scaffolding components
import { 
  PageSection, 
  Card, 
  CardHeader,
  CardBody,
  FormSelect, 
  FormSelectOption,
  Title,
  Button,
  Gallery,
  GalleryItem,
  HelperText,
  HelperTextItem,
  Checkbox,
  TextInput,
  FormGroup,
  Radio,
  Page,
  Masthead,
  MastheadMain,
  MastheadBrand,
  TextArea
} from '@patternfly/react-core';

// 2. Structural design system icons
import { CheckCircleIcon, InfrastructureIcon, NetworkIcon, ClusterIcon, AddCircleOIcon, TrashIcon, AutomationIcon, KeyIcon, RedhatIcon } from '@patternfly/react-icons';
import { highlightYamlTokens } from './highlightYaml.js';

const API_TOKEN_STORAGE_KEY = 'ocpMetalUiApiToken';
let apiTokenValue = '';

function readStoredApiToken() {
  try {
    return sessionStorage.getItem(API_TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function storeApiToken(value) {
  const token = value.trim();
  apiTokenValue = token;
  try {
    if (token) sessionStorage.setItem(API_TOKEN_STORAGE_KEY, token);
    else sessionStorage.removeItem(API_TOKEN_STORAGE_KEY);
  } catch {
    /* The token still applies to requests in this page load. */
  }
  window.dispatchEvent(new Event('ocp-api-token'));
}

apiTokenValue = readStoredApiToken();

function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiTokenValue && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${apiTokenValue}`);
  }
  return fetch(url, { ...options, headers });
}

function ApiTokenField() {
  const [token, setToken] = useState(apiTokenValue);
  const [saved, setSaved] = useState(Boolean(apiTokenValue));
  return (
    <FormGroup label="API token" fieldId="api-token">
      <TextInput
        id="api-token"
        type="password"
        autoComplete="off"
        value={token}
        onChange={(_event, value) => {
          setToken(value);
          setSaved(false);
        }}
      />
      <Button
        variant="secondary"
        className="pf-v6-u-mt-sm"
        onClick={() => {
          storeApiToken(token);
          setSaved(true);
        }}
      >
        Use token
      </Button>
      <HelperText>
        <HelperTextItem>
          {saved
            ? 'Token kept for this browser tab. It is not sent to the database.'
            : 'Paste the token from the deploy host, then choose Use token.'}
        </HelperTextItem>
      </HelperText>
    </FormGroup>
  );
}

// Core stylesheets required for official compilation layout rules
import '@patternfly/react-core/dist/styles/base.css';
import '@patternfly/react-styles/css/components/Wizard/wizard.css';
import '@patternfly/react-styles/css/components/Card/card.css';
import '@patternfly/react-styles/css/components/Page/page.css';
import '@patternfly/react-styles/css/components/Masthead/masthead.css';

const NODE_ROLES = ['master', 'worker', 'storage'];
const ROLE_LABELS = { master: 'Master', worker: 'Worker', storage: 'Storage' };

function selectedRoles(config = {}) {
  if (Array.isArray(config.roles)) return config.roles;
  if (config.role === 'master' || config.role === 'worker') return [config.role];
  return [];
}

function roleLabel(config) {
  const roles = selectedRoles(config);
  if (!roles.length) return 'Not chosen';
  return roles.map((role) => ROLE_LABELS[role] || role).join(', ');
}

function assignedVlan(config) {
  const enabled = config.netType === 'bond' ? config.bondVlanEnabled : config.singleVlanEnabled;
  const vlanId = config.netType === 'bond' ? config.bondVlanId : config.singleVlanId;
  const text = String(vlanId ?? '').trim();
  return enabled && text ? text : '';
}

function hostAddress(octet, machineNetworkCidr, currentIp) {
  const text = String(octet ?? '').trim();
  const network = String(machineNetworkCidr || '').split('/')[0];
  const parts = network.split('.');
  if (
    /^\d{1,3}$/.test(text)
    && Number(text) <= 255
    && parts.length === 4
    && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
  ) {
    return [...parts.slice(0, -1), String(Number(text))].join('.');
  }
  return currentIp || 'From IPA';
}

function findFastestDrive(disks) {
  if (!disks || disks.length === 0) return '';
  return disks.reduce((bestDisk, disk) => {
    const score = (model) => {
      const modelLower = model.toLowerCase();
      if (modelLower.includes('nvme')) return 3;
      if (modelLower.includes('ssd')) return 2;
      return 1;
    };
    return score(disk.model) > score(bestDisk.model) ||
      (score(disk.model) === score(bestDisk.model) && disk.size > bestDisk.size)
      ? disk
      : bestDisk;
  }).serial;
}

function normalizeLinkStatus(nic) {
  const reportedStatus = nic.link_status
    ?? nic.linkStatus
    ?? nic.link_state
    ?? nic.linkState
    ?? nic.operstate
    ?? nic.state
    ?? nic.is_up
    ?? nic.has_carrier
    ?? nic.up;
  if (typeof reportedStatus === 'boolean') return reportedStatus ? 'up' : 'down';
  if (typeof reportedStatus !== 'string') return 'unknown';
  const status = reportedStatus.toLowerCase();
  if (['up', 'linkup', 'enabled', 'true', '1', 'yes'].includes(status)) return 'up';
  if (['down', 'linkdown', 'nolink', 'disabled', 'false', '0', 'no'].includes(status)) return 'down';
  return 'unknown';
}

function NicLinkIndicator({ status }) {
  const color = status === 'up' ? '#3e8635' : status === 'down' ? '#c9190b' : '#8a8d90';
  return (
    <span
      role="img"
      aria-label={`Link ${status}`}
      title={`Link ${status}`}
      style={{
        display: 'inline-block',
        width: '10px',
        height: '10px',
        flexShrink: 0,
        backgroundColor: color,
        borderRadius: '2px',
        marginRight: '8px',
        verticalAlign: 'middle'
      }}
    />
  );
}

function normalizeNode(node) {
  const inventory = node.inventory || {};
  const cpu = inventory.cpus || inventory.cpu || {};
  const memory = inventory.memory || {};
  const rawInterfaces = inventory.interfaces || [];
  const interfaces = Array.isArray(rawInterfaces)
    ? rawInterfaces
    : Object.entries(rawInterfaces).map(([name, details]) => ({ name, ...details }));

  return {
    ...node,
    inventory: {
      ...inventory,
      cpus: {
        count: Number(cpu.count || cpu.cores || 0),
        model: cpu.model || cpu.model_name || 'Unknown CPU'
      },
      memory: {
        total: Number(memory.total || 0) || Number(memory.physical_mb || 0) * 1024 * 1024
      },
      disks: (Array.isArray(inventory.disks) ? inventory.disks : []).map((disk, index) => ({
        ...disk,
        serial: String(disk.serial || disk.name || `disk-${index + 1}`),
        model: disk.model || 'Unknown disk',
        size: Number(disk.size || disk.size_bytes || 0)
      })),
      interfaces: interfaces.map((nic, index) => ({
        ...nic,
        name: nic.name || `interface-${index + 1}`,
        mac: nic.mac || nic.mac_address || 'MAC unavailable',
        linkStatus: normalizeLinkStatus(nic)
      }))
    }
  };
}

function defaultAnsibleHost(node) {
  const host = (node.serial_number || node.uuid.slice(0, 8))
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '');
  return host || node.uuid.slice(0, 8);
}

function routeFromHash(hash = window.location.hash) {
  const route = hash.replace(/^#\/?/, '').split('/');
  if (route[0] === 'pxe' || route[0] === 'redfish') {
    return {
      type: 'wizard',
      strategy: route[0],
      stage: ['blueprint', 'bmc', 'inventory', 'discovered', 'review'].includes(route[1])
        ? route[1]
        : 'blueprint'
    };
  }
  return { type: 'landing' };
}

function routeKey(route) {
  return route.type === 'landing' ? 'landing' : `${route.strategy}/${route.stage}`;
}

function routeHash(route) {
  return route.type === 'landing' ? '#/' : `#/${route.strategy}/${route.stage}`;
}

const DEFAULT_BLUEPRINT = {
  clusterName: 'ocp-baremetal',
  baseDomain: 'ocp.internal',
  controlPlaneTopology: 'Compact',
  mirrorRegistries: ['registry.airgap.internal:5000'],
  caCertBundle: '',
  machineNetworkCidr: '192.0.2.0/24',
  dnsServers: ['192.0.2.35'],
  ntpServers: [],
  genesisHostname: 'edge',
  genesisIp: '192.0.2.35',
  genesisNic: 'eno5',
  genesisNics: ['eno5'],
  genesisDisks: [],
  networkGateway: '192.0.2.1',
  mgmtNetworkCidr: '198.51.100.0/24',
  mgmtVlan: '280',
  mgmtDc01Octet: '6',
  mgmtDc02Octet: '7',
  apiVip: '',
  ingressVip: ''
};

export default function App() {
  const [nodes, setNodes] = useState([]);
  const [nodeConfigs, setNodeConfigs] = useState({});
  const [page, setPage] = useState(() => routeFromHash());
  const [inventoryStatus, setInventoryStatus] = useState('Loading hardware inventory...');
  const [inventoryError, setInventoryError] = useState('');
  const [isRefreshingInventory, setIsRefreshingInventory] = useState(false);
  const [outputDirectory, setOutputDirectory] = useState('');
  const [ansibleWorkingDir, setAnsibleWorkingDir] = useState('');
  const [newNodeName, setNewNodeName] = useState('');
  const [discoveryAssignments, setDiscoveryAssignments] = useState({});

  const [blueprint, setBlueprint] = useState(DEFAULT_BLUEPRINT);
  const blueprintRef = useRef(blueprint);
  useEffect(() => {
    blueprintRef.current = blueprint;
  }, [blueprint]);
  const [redfishEndpoints, setRedfishEndpoints] = useState([]);
  const [httpsEnabled, setHttpsEnabled] = useState(false);
  const [isSavingConfiguration, setIsSavingConfiguration] = useState(false);
  const [configPreview, setConfigPreview] = useState(null);
  const [yamlView, setYamlView] = useState('');

  const navigateToPage = (nextPage, replace = false) => {
    const previousRoute = routeKey(page);
    const nextRoute = routeKey(nextPage);
    const method = replace ? 'replaceState' : 'pushState';
    window.history[method](
      { metalUiRoute: nextRoute, metalUiPrevious: replace ? null : previousRoute },
      '',
      `${window.location.pathname}${window.location.search}${routeHash(nextPage)}`
    );
    setPage(nextPage);
  };

  useEffect(() => {
    const currentPage = routeFromHash();
    window.history.replaceState(
      { ...window.history.state, metalUiRoute: routeKey(currentPage), metalUiPrevious: null },
      '',
      window.location.href
    );
    const restorePage = () => setPage(routeFromHash());
    window.addEventListener('popstate', restorePage);
    window.addEventListener('hashchange', restorePage);
    return () => {
      window.removeEventListener('popstate', restorePage);
      window.removeEventListener('hashchange', restorePage);
    };
  }, []);

  const openNodeReview = () => {
    navigateToPage({ type: 'wizard', strategy: page.strategy, stage: 'review' });
  };

  const openDiscoveredNodes = () => {
    navigateToPage({ type: 'wizard', strategy: page.strategy, stage: 'discovered' });
  };

  const returnToInventory = () => {
    navigateToPage({ type: 'wizard', strategy: page.strategy, stage: 'inventory' }, true);
  };

  const commitConfiguration = async () => {
    setInventoryError('');
    setInventoryStatus('Saving configuration to PostgreSQL...');
    setIsSavingConfiguration(true);
    try {
      const response = await apiFetch('/api/v1/configuration', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: nodeConfigs, blueprint, outputDirectory })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Configuration could not be saved (HTTP ${response.status}).`);
      setInventoryStatus(`Configuration saved to PostgreSQL for ${result.savedNodes} node${result.savedNodes === 1 ? '' : 's'}.`);
    } catch (error) {
      setInventoryError(error.message);
      setInventoryStatus('Configuration save failed');
    } finally {
      setIsSavingConfiguration(false);
    }
  };

  const loadInventory = useCallback(async () => {
    const response = await apiFetch('/api/v1/nodes');
    if (!response.ok) throw new Error(`Inventory API returned HTTP ${response.status}.`);
    const loadedNodes = (await response.json()).map(normalizeNode);
    setNodes(loadedNodes);
    setNodeConfigs((current) => Object.fromEntries(loadedNodes.map((node) => {
      const savedPreferences = Object.fromEntries(
        Object.entries(node.preferences || {}).filter(([key]) => key !== 'ansibleGroup')
      );
      if (!Array.isArray(savedPreferences.roles) && (savedPreferences.role === 'master' || savedPreferences.role === 'worker')) {
        savedPreferences.roles = [savedPreferences.role];
      }
      const currentPreferences = Object.fromEntries(
        Object.entries(current[node.uuid] || {}).filter(([key]) => key !== 'ansibleGroup')
      );
      return [node.uuid, {
        rootDisk: findFastestDrive(node.inventory.disks),
        netType: 'single',
        singleNic: blueprintRef.current.genesisNic || 'eno5',
        singleVlanEnabled: true,
        singleVlanId: '281',
        bondNics: [],
        bondVlanEnabled: true,
        bondVlanId: '281',
        ...savedPreferences,
        ...currentPreferences,
        ansibleHost: currentPreferences.ansibleHost
          || savedPreferences.ansibleHost
          || (node.is_placeholder ? defaultAnsibleHost(node) : '')
      }];
    })));
    return loadedNodes;
  }, []);

  const refreshInventory = async () => {
    setIsRefreshingInventory(true);
    setInventoryError('');
    setInventoryStatus('Refreshing inventory from PostgreSQL...');
    try {
      const loadedNodes = await loadInventory();
      const discoveredCount = loadedNodes.filter((node) => !node.is_placeholder).length;
      setInventoryStatus(
        `Inventory refreshed: ${discoveredCount} discovered hardware node${discoveredCount === 1 ? '' : 's'}`
      );
    } catch (error) {
      setInventoryError(error.message);
      setInventoryStatus('Inventory refresh failed');
    } finally {
      setIsRefreshingInventory(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const initialize = async () => {
      try {
        const configResponse = await apiFetch('/api/v1/config');
        if (!configResponse.ok) throw new Error(`Configuration API returned HTTP ${configResponse.status}.`);
        const config = await configResponse.json();
        if (cancelled) return;
        setAnsibleWorkingDir(config.ansibleWorkingDir);
        setOutputDirectory(config.outputDirectory || config.defaultOutputDirectory);
        setBlueprint(config.blueprint);
        setRedfishEndpoints((config.redfishEndpoints || []).map(({ id, name, baseUrl }) => ({
          id,
          name,
          baseUrl
        })));
        setHttpsEnabled(Boolean(config.httpsEnabled));
        const loadedNodes = await loadInventory();
        if (!cancelled) {
          setInventoryError('');
          setInventoryStatus(
            loadedNodes.length
              ? `${loadedNodes.length} node${loadedNodes.length === 1 ? '' : 's'} loaded from PostgreSQL`
              : 'Inventory is empty; add named nodes or wait for IPA reports'
          );
        }
      } catch (error) {
        if (!cancelled) {
          setInventoryError(error.message);
          setInventoryStatus('Inventory unavailable');
        }
      }
    };
    const refresh = async () => {
      try {
        const loadedNodes = await loadInventory();
        if (!cancelled) {
          setInventoryError('');
          setInventoryStatus(
            loadedNodes.length
              ? `${loadedNodes.length} node${loadedNodes.length === 1 ? '' : 's'} loaded from PostgreSQL`
              : 'Inventory is empty; add named nodes or wait for IPA reports'
          );
        }
      } catch (error) {
        if (!cancelled) {
          setInventoryError(error.message);
          setInventoryStatus('Inventory unavailable');
        }
      }
    };
    initialize();
    const refreshTimer = window.setInterval(refresh, 15000);
    const onToken = () => {
      initialize();
    };
    window.addEventListener('ocp-api-token', onToken);
    return () => {
      cancelled = true;
      window.clearInterval(refreshTimer);
      window.removeEventListener('ocp-api-token', onToken);
    };
  }, [loadInventory]);

  const updateBlueprintField = (field, value) => {
    setBlueprint(prev => ({ ...prev, [field]: value }));
  };

  const handleAddRedfishEndpoint = () => {
    const endpointId = globalThis.crypto?.randomUUID?.() || `endpoint-${Date.now()}`;
    setRedfishEndpoints((current) => [
      ...current,
      {
        id: endpointId,
        name: '',
        baseUrl: 'https://'
      }
    ]);
  };

  const handleRedfishChange = (id, field, value) => {
    setRedfishEndpoints((current) => current.map((endpoint) => (
      endpoint.id === id ? { ...endpoint, [field]: value } : endpoint
    )));
  };

  const handleRemoveRedfishEndpoint = (id) => {
    setRedfishEndpoints((current) => current.filter((endpoint) => endpoint.id !== id));
  };

  const saveRedfishEndpoints = async () => {
    const response = await apiFetch('/api/v1/redfish/endpoints', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoints: redfishEndpoints })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Could not save BMC profiles (HTTP ${response.status}).`);
    setRedfishEndpoints(result.endpoints);
  };

  const retrieveRedfishInventory = async (endpointId, username, password, allowUntrustedTls) => {
    await saveRedfishEndpoints();
    const response = await apiFetch('/api/v1/redfish/retrieve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpointId, username, password, allowUntrustedTls })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Redfish retrieval failed (HTTP ${response.status}).`);
    await loadInventory();
    return result;
  };

  const addNamedNode = async () => {
    setInventoryError('');
    try {
      const response = await apiFetch('/api/v1/nodes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ansibleHost: newNodeName })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Could not add node (HTTP ${response.status}).`);
      setNewNodeName('');
      await loadInventory();
      setInventoryStatus(`Named host ${result.preferences.ansibleHost} added`);
    } catch (error) {
      setInventoryError(error.message);
    }
  };

  const updateConfigField = (uuid, field, value) => {
    setNodeConfigs((prev) => {
      const next = { ...prev, [uuid]: { ...prev[uuid], [field]: value } };
      if (field === 'roles' && !(value || []).includes('master')) {
        next[uuid] = { ...next[uuid], rendezvous: false };
      }
      if (field === 'rendezvous' && value === true) {
        for (const [otherId, config] of Object.entries(next)) {
          if (otherId !== uuid) next[otherId] = { ...config, rendezvous: false };
        }
      }
      return next;
    });
  };

  const updateDiscoveryAssignment = (nodeUuid, field, value) => {
    setDiscoveryAssignments((current) => {
      const updated = { ...current };
      if (field === 'clear' || (field === 'ansibleHost' && !value.trim())) {
        delete updated[nodeUuid];
      } else {
        updated[nodeUuid] = field === 'unlink' ? { unlink: true } : { [field]: value };
      }
      return updated;
    });
  };

  const updateDiscoveredInventory = async () => {
    const assignments = Object.entries(discoveryAssignments).flatMap(([nodeUuid, assignment]) => {
      if (assignment.unlink) return [{ nodeUuid, unlink: true }];
      if (assignment.placeholderUuid) return [{ nodeUuid, placeholderUuid: assignment.placeholderUuid }];
      if (assignment.ansibleHost?.trim()) return [{ nodeUuid, ansibleHost: assignment.ansibleHost.trim() }];
      return [];
    });
    if (!assignments.length) {
      setInventoryStatus('Choose a node or inventory name to link, unlink a node, or clear an assignment.');
      return;
    }

    setInventoryError('');
    setInventoryStatus('Updating inventory assignments...');
    try {
      const response = await apiFetch('/api/v1/nodes/assignments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assignments })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Inventory assignments failed (HTTP ${response.status}).`);
      await loadInventory();
      setDiscoveryAssignments({});
      setInventoryStatus(`Updated links for ${result.updated} node${result.updated === 1 ? '' : 's'}.`);
    } catch (error) {
      setInventoryError(error.message);
      setInventoryStatus('Inventory update failed');
    }
  };

  const deleteInventoryNode = async (node) => {
    const nodeName = node.preferences?.ansibleHost || node.serial_number || node.uuid;
    if (!globalThis.confirm(`Delete ${node.is_placeholder ? 'inventory item' : 'discovered node'} "${nodeName}"?`)) return;
    setInventoryError('');
    try {
      const response = await apiFetch(`/api/v1/nodes/${encodeURIComponent(node.uuid)}`, { method: 'DELETE' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Could not delete node (HTTP ${response.status}).`);
      await loadInventory();
      setDiscoveryAssignments((current) => {
        const updated = { ...current };
        delete updated[node.uuid];
        for (const [discoveredUuid, assignment] of Object.entries(updated)) {
          if (assignment.placeholderUuid === node.uuid) delete updated[discoveredUuid];
        }
        return updated;
      });
      setInventoryStatus(`Deleted ${node.is_placeholder ? 'inventory item' : 'discovered node'} ${nodeName}.`);
    } catch (error) {
      setInventoryError(error.message);
      setInventoryStatus('Node deletion failed');
    }
  };

  const handleBondNicToggle = (uuid, nicName, isChecked) => {
    setNodeConfigs(prev => {
      const currentBonds = prev[uuid].bondNics || [];
      const updatedBonds = isChecked ? [...currentBonds, nicName] : currentBonds.filter(name => name !== nicName);
      return { ...prev, [uuid]: { ...prev[uuid], bondNics: updatedBonds } };
    });
  };

  const commitReviewedConfiguration = async () => {
    setInventoryError('');
    setConfigPreview(null);
    setYamlView('');
    setInventoryStatus('Committing reviewed inventory and preferences...');
    try {
      const saveResponse = await apiFetch('/api/v1/ansible-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: nodeConfigs, blueprint, outputDirectory })
      });
      if (!saveResponse.ok) {
        const error = await saveResponse.json();
        throw new Error(error.error || `Ansible files could not be committed (HTTP ${saveResponse.status}).`);
      }
      const result = await saveResponse.json();
      const installConfigFile = result.installConfigFile || '';
      const agentConfigFile = result.agentConfigFile || '';
      let statusText = `Reviewed files committed. Inventory: ${result.inventoryFile}`;
      if (installConfigFile && agentConfigFile) {
        statusText = `Reviewed files committed. Wrote ${installConfigFile} and ${agentConfigFile}.`;
      } else if (result.installFilesMessage) {
        statusText = `Reviewed files committed. Inventory: ${result.inventoryFile}. ${result.installFilesMessage}`;
      }
      setInventoryStatus(statusText);
      setConfigPreview({
        installConfig: result.installConfig || '',
        agentConfig: result.agentConfig || '',
        error: result.configPreviewError || '',
        installConfigFile,
        agentConfigFile,
        installFilesMessage: result.installFilesMessage || ''
      });
    } catch (error) {
      setInventoryError(error.message);
      setInventoryStatus('Save or export failed');
    }
  };
  const header = (
    <Masthead backgroundColor="dark" style={{ borderBottom: '2px solid #ee0000' }}>
      <MastheadMain>
        <MastheadBrand style={{ color: '#ffffff', display: 'flex', alignItems: 'center', gap: '10px', textDecoration: 'none' }}>
          <RedhatIcon style={{ color: '#ffffff', fontSize: '1.6rem' }} />
          <span style={{ color: '#d2d2d2', fontWeight: '300' }}>OpenShift Bare-Metal Builder</span>
        </MastheadBrand>
      </MastheadMain>
    </Masthead>
  );

  return (
    <>
    <Page header={header} sidebar={null} className="openshift-builder-wizard" style={{ height: '100vh' }}>
      <div style={{ height: 'calc(100vh - 70px)', display: 'flex', flexDirection: 'column' }}>
        {page.type === 'landing' && (
          <StrategyLandingScreen
            onStart={() => navigateToPage({ type: 'wizard', strategy: 'pxe', stage: 'blueprint' })}
            onOpenBmc={() => navigateToPage({ type: 'wizard', strategy: 'pxe', stage: 'bmc' })}
          />
        )}

        {page.type === 'wizard' && (
          <div className="workflow-layout" style={{ display: 'flex', flex: 1, minHeight: 0 }}>
            <WorkflowSidebar
              activeStage={page.stage}
              onNavigate={(stage) => navigateToPage({
                type: 'wizard',
                strategy: page.strategy,
                stage
              })}
            />
            <section aria-label="Workflow content" style={{ flex: 1, minWidth: 0, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
              {page.stage === 'blueprint' && (
                <>
                  <BlueprintStep
                    blueprint={blueprint}
                    onChangeField={updateBlueprintField}
                    onChangeListField={(field, value) => updateBlueprintField(
                      field,
                      value.split(/[\n,]/).map((entry) => entry.trim()).filter(Boolean)
                    )}
                    outputDirectory={outputDirectory}
                    onOutputDirectoryChange={setOutputDirectory}
                    ansibleWorkingDir={ansibleWorkingDir}
                    httpsEnabled={httpsEnabled}
                    onAddRegistry={() => setBlueprint((current) => ({ ...current, mirrorRegistries: [...current.mirrorRegistries, ''] }))}
                    onRegistryChange={(index, value) => setBlueprint((current) => {
                      const registries = [...current.mirrorRegistries];
                      registries[index] = value;
                      return { ...current, mirrorRegistries: registries };
                    })}
                    onRemoveRegistry={(index) => setBlueprint((current) => ({
                      ...current,
                      mirrorRegistries: current.mirrorRegistries.filter((_, itemIndex) => itemIndex !== index)
                    }))}
                  />
                  <div className="workflow-actions">
                    <div>
                      <Button variant="secondary" onClick={commitConfiguration} isDisabled={isSavingConfiguration}>
                        {isSavingConfiguration ? 'Committing...' : 'Commit Blueprint'}
                      </Button>
                      <p role={inventoryError ? 'alert' : 'status'} className="workflow-action-status">
                        {inventoryError || inventoryStatus}
                      </p>
                    </div>
                    <Button variant="primary" onClick={openDiscoveredNodes}>
                      Continue to Discovered Nodes
                    </Button>
                  </div>
                </>
              )}
              {page.stage === 'bmc' && (
                <RedfishManagementStep
                  endpoints={redfishEndpoints}
                  httpsEnabled={httpsEnabled}
                  onAddEndpoint={handleAddRedfishEndpoint}
                  onEndpointChange={handleRedfishChange}
                  onRemoveEndpoint={handleRemoveRedfishEndpoint}
                  onSaveEndpoints={saveRedfishEndpoints}
                  onRetrieve={retrieveRedfishInventory}
                  onReviewNodes={openDiscoveredNodes}
                />
              )}
              {page.stage === 'review' && (
                <InventoryReviewPage
                  nodes={nodes}
                  configs={nodeConfigs}
                  machineNetworkCidr={blueprint.machineNetworkCidr}
                  outputDirectory={outputDirectory}
                  status={inventoryStatus}
                  error={inventoryError}
                  isRefreshing={isRefreshingInventory}
                  onRefresh={refreshInventory}
                  onBack={returnToInventory}
                  onConfirm={commitReviewedConfiguration}
                  configPreview={configPreview}
                  onViewYaml={setYamlView}
                />
              )}
              {page.stage === 'discovered' && (
                <DiscoveredNodesPage
                  nodes={nodes}
                  assignments={discoveryAssignments}
                  onAssignmentChange={updateDiscoveryAssignment}
                  inventoryItems={nodes.filter((node) => node.is_placeholder)}
                  onDeleteNode={deleteInventoryNode}
                  onAddInventoryItem={addNamedNode}
                  newNodeName={newNodeName}
                  onNewNodeNameChange={setNewNodeName}
                  onRefresh={refreshInventory}
                  onUpdateInventory={updateDiscoveredInventory}
                  onBack={returnToInventory}
                  isRefreshing={isRefreshingInventory}
                  status={inventoryStatus}
                  error={inventoryError}
                />
              )}
              {page.stage === 'inventory' && (
                <InventoryStep
                  nodes={nodes}
                  configs={nodeConfigs}
                  onChangeField={updateConfigField}
                  onBondToggle={handleBondNicToggle}
                  onReview={openNodeReview}
                  onCommitConfiguration={commitConfiguration}
                  isSavingConfiguration={isSavingConfiguration}
                  status={inventoryStatus}
                  error={inventoryError}
                />
              )}
            </section>
          </div>
        )}

      </div>
    </Page>
    {yamlView && (
      <YamlPopout
        which={yamlView}
        installConfig={configPreview?.installConfig || ''}
        agentConfig={configPreview?.agentConfig || ''}
        written={Boolean(configPreview?.installConfigFile)}
        onChoose={setYamlView}
        onClose={() => setYamlView('')}
      />
    )}
    </>
  );
}

function WorkflowSidebar({ activeStage, onNavigate }) {
  const sections = [
    { stage: 'blueprint', label: 'Global Cluster Blueprint' },
    { stage: 'bmc', label: 'BMC / iLO Profiles' },
    { stage: 'discovered', label: 'Discovered Nodes' },
    { stage: 'inventory', label: 'Inventory Config' },
    { stage: 'review', label: 'Review' }
  ];

  return (
    <aside aria-label="Inventory workflow navigation" className="workflow-sidebar">
      <div className="workflow-sidebar-brand">
        <RedhatIcon aria-hidden="true" />
        <span>BUILDER</span>
      </div>
      <strong className="workflow-sidebar-heading">
        Workflow
      </strong>
      <nav aria-label="Workflow pages" style={{ display: 'grid', gap: '4px' }}>
        {sections.map(({ stage, label }) => {
          const isActive = activeStage === stage;
          return (
            <Button
              key={stage}
              variant="link"
              className={`workflow-nav-button${isActive ? ' is-active' : ''}`}
              aria-current={isActive ? 'page' : undefined}
              onClick={() => onNavigate(stage)}
            >
              {label}
            </Button>
          );
        })}
      </nav>
      <div className="pf-v6-u-mt-md">
        <ApiTokenField />
      </div>
    </aside>
  );
}

function StrategyLandingScreen({ onStart, onOpenBmc }) {
  return (
    <PageSection variant="default" className="splash-page">
      <div className="splash-content">
        <section className="splash-hero" aria-labelledby="splash-title">
          <div className="splash-copy">
            <div className="splash-brand">
              <RedhatIcon aria-hidden="true" />
              <span>OPENSHIFT BARE-METAL BUILDER</span>
            </div>
            <p className="splash-eyebrow">AUTOMATED HARDWARE DISCOVERY &amp; OPENSHIFT INSTALL CONFIGURATION</p>
            <Title id="splash-title" headingLevel="h1" size="4xl" className="splash-title">
              From bare metal to OpenShift-ready.
            </Title>
            <p className="splash-description">
              Discover your servers, shape the install configuration, and prepare
              Ansible inventory and variables in one streamlined workflow.
            </p>
            <div className="splash-actions">
              <Button variant="primary" onClick={onStart} icon={<AutomationIcon />}>
                Start cluster configuration
              </Button>
              <Button variant="secondary" onClick={onOpenBmc} icon={<InfrastructureIcon />}>
                Manage BMC / iLO profiles
              </Button>
            </div>
            <p className="splash-note">
              Built for disconnected environments. Your hardware data aligned with automation.
            </p>
            <ApiTokenField />
          </div>
          <div className="splash-workflow" aria-label="Workflow overview">
            <div className="splash-workflow-heading">A clear path to deployment</div>
            <div className="splash-flow-step">
              <span className="splash-step-number">01</span>
              <span className="splash-step-icon"><AutomationIcon /></span>
              <span><strong>Discover hardware</strong><small>PXE or iLO</small></span>
            </div>
            <div className="splash-flow-connector" />
            <div className="splash-flow-step">
              <span className="splash-step-number">02</span>
              <span className="splash-step-icon"><InfrastructureIcon /></span>
              <span><strong>Configure inventory</strong><small>Match hosts, disks, and networks</small></span>
            </div>
            <div className="splash-flow-connector" />
            <div className="splash-flow-step">
              <span className="splash-step-number">03</span>
              <span className="splash-step-icon"><CheckCircleIcon /></span>
              <span><strong>Review and generate</strong><small>Export Ansible-ready files</small></span>
            </div>
          </div>
        </section>
        <div className="splash-capabilities">
          <article>
            <span className="splash-capability-icon"><AutomationIcon /></span>
            <div><strong>Flexible discovery</strong><p>Collect inventory through Ironic Python Agent or Redfish-capable BMCs.</p></div>
          </article>
          <article>
            <span className="splash-capability-icon"><NetworkIcon /></span>
            <div><strong>One shared workflow</strong><p>Bring discovered nodes together for host assignment and hardware preferences.</p></div>
          </article>
          <article>
            <span className="splash-capability-icon"><ClusterIcon /></span>
            <div><strong>Install-ready output</strong><p>Generate a clean inventory and host/group variables for your Ansible environment.</p></div>
          </article>
        </div>
        <footer className="splash-footer">
          <span>Designed for OpenShift bare-metal installation automation</span>
        </footer>
      </div>
    </PageSection>
  );
}
const EMPTY_INSTALL_SECRET_STATUS = {
  pullSecretReady: false,
  pullSecretPath: '',
  pullSecretError: '',
  privateKeyStored: false,
  sshPublicKey: ''
};

function InstallSecrets({ outputDirectory, httpsEnabled }) {
  const fileInputRef = useRef(null);
  const [status, setStatus] = useState(EMPTY_INSTALL_SECRET_STATUS);
  const [pullSecretText, setPullSecretText] = useState('');
  const [publicKeyText, setPublicKeyText] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const canSave = Boolean(outputDirectory) && httpsEnabled && !busy;

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (!outputDirectory) {
        setStatus(EMPTY_INSTALL_SECRET_STATUS);
        return;
      }
      try {
        const response = await apiFetch(
          `/api/v1/install-secrets?outputDirectory=${encodeURIComponent(outputDirectory)}`
        );
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || `Could not read install secret status (HTTP ${response.status}).`);
        }
        if (!cancelled) {
          setStatus({
            pullSecretReady: Boolean(result.pullSecretReady),
            pullSecretPath: typeof result.pullSecretPath === 'string'
              ? result.pullSecretPath
              : EMPTY_INSTALL_SECRET_STATUS.pullSecretPath,
            pullSecretError: typeof result.pullSecretError === 'string' ? result.pullSecretError : '',
            privateKeyStored: Boolean(result.privateKeyStored),
            sshPublicKey: typeof result.sshPublicKey === 'string' ? result.sshPublicKey : ''
          });
        }
      } catch (loadError) {
        if (!cancelled) setError(loadError.message);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [outputDirectory]);

  const applyStatus = (result) => {
    setStatus({
      pullSecretReady: Boolean(result.pullSecretReady),
      pullSecretPath: typeof result.pullSecretPath === 'string'
        ? result.pullSecretPath
        : EMPTY_INSTALL_SECRET_STATUS.pullSecretPath,
      pullSecretError: typeof result.pullSecretError === 'string' ? result.pullSecretError : '',
      privateKeyStored: Boolean(result.privateKeyStored),
      sshPublicKey: typeof result.sshPublicKey === 'string' ? result.sshPublicKey : ''
    });
  };

  const postSecret = async (path, body) => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const response = await apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Request failed (HTTP ${response.status}).`);
      applyStatus(result);
      return result;
    } catch (postError) {
      setError(postError.message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const savePullSecret = async () => {
    const text = pullSecretText.trim();
    if (!text) {
      setError('The pull secret is empty.');
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError('The pull secret is not valid JSON.');
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) || !parsed.auths || typeof parsed.auths !== 'object' || Array.isArray(parsed.auths)) {
      setError('The pull secret must contain an auths object.');
      return;
    }
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const response = await apiFetch('/api/v1/install-secrets/pull-secret', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pullSecret: text })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Request failed (HTTP ${response.status}).`);
      setPullSecretText('');
      if (fileInputRef.current) fileInputRef.current.value = '';
      setMessage(`Pull secret saved to ${result.path}.`);
      const statusResponse = await apiFetch(
        `/api/v1/install-secrets?outputDirectory=${encodeURIComponent(outputDirectory)}`
      );
      const statusResult = await statusResponse.json();
      if (statusResponse.ok) applyStatus(statusResult);
    } catch (saveError) {
      setError(saveError.message);
    } finally {
      setBusy(false);
    }
  };

  const onPullSecretFile = (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      setPullSecretText(typeof reader.result === 'string' ? reader.result : '');
      setError('');
      setMessage('Pull secret file loaded. Save it to write the host file.');
    };
    reader.onerror = () => setError('Could not read that file.');
    reader.readAsText(file);
  };

  const createSshKey = async () => {
    if (status.privateKeyStored && !window.confirm('Create a new SSH key? The previous private key will no longer be offered for download.')) {
      return;
    }
    const saved = await postSecret('/api/v1/install-secrets/ssh-key', {
      outputDirectory,
      replace: Boolean(status.privateKeyStored)
    });
    if (!saved) return;
    setMessage('SSH key created. Download the private key and keep it. The public key is shown here.');
  };

  const usePublicKey = async () => {
    const text = publicKeyText.trim();
    if (!text) {
      setError('Paste one public key line.');
      return;
    }
    if (text.includes('PRIVATE KEY') || text.includes('-----BEGIN')) {
      setError('That looks like a private key. Paste the public key, one line that starts with ssh-ed25519, ssh-rsa, or ecdsa-sha2-nistp256.');
      return;
    }
    const lines = text.split(/\r?\n/).filter(Boolean);
    if (lines.length !== 1) {
      setError('Paste one public key line that starts with ssh-ed25519, ssh-rsa, or ecdsa-sha2-nistp256.');
      return;
    }
    let replace = false;
    if (status.privateKeyStored && status.sshPublicKey !== text) {
      if (!window.confirm('Use this public key instead? The stored private key will be removed because it will no longer match.')) return;
      replace = true;
    }
    const saved = await postSecret('/api/v1/install-secrets/ssh-public-key', {
      outputDirectory,
      sshPublicKey: text,
      replace
    });
    if (!saved) return;
    setPublicKeyText('');
    setMessage(replace
      ? 'Public key saved. The previous private key was removed.'
      : 'Public key saved for this cluster.');
  };

  const downloadPrivateKey = async () => {
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const response = await apiFetch(
        `/api/v1/install-secrets/ssh-private-key?outputDirectory=${encodeURIComponent(outputDirectory)}`
      );
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(result.error || `Could not download the private key (HTTP ${response.status}).`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'id_ed25519';
      link.click();
      URL.revokeObjectURL(url);
      setMessage('Private key download started. Keep that file. This page does not show the private key.');
    } catch (downloadError) {
      setError(downloadError.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="install-secrets">
      <FormGroup label="Cluster pull secret" fieldId="bp-pull-secret">
        <TextArea
          id="bp-pull-secret"
          style={{ resize: 'vertical', minHeight: '80px', fontFamily: 'monospace', fontSize: '0.85rem' }}
          value={pullSecretText}
          onChange={(_event, value) => setPullSecretText(value)}
          placeholder='{"auths":{"registry.example.com":{"auth":"..."}}}'
          spellCheck={false}
          autoComplete="off"
        />
        <HelperText>
          <HelperTextItem>
            {status.pullSecretReady
              ? `Using the pull secret at ${status.pullSecretPath}. Paste a replacement to save it there. The UI reads the file again on the next generate.`
              : (status.pullSecretError
                || `The pull secret file ${status.pullSecretPath} is not available. Paste one to save it there.`)}
          </HelperTextItem>
        </HelperText>
        <div className="install-secret-actions">
          <input
            id="bp-pull-secret-file"
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            aria-label="Choose pull-secret.json"
            onChange={onPullSecretFile}
          />
          <Button id="bp-pull-secret-save" variant="secondary" onClick={savePullSecret} isDisabled={!canSave}>
            Save pull secret
          </Button>
        </div>
        <HelperText>
          <HelperTextItem>
            Save writes the host file. This page does not show the saved secret.
          </HelperTextItem>
        </HelperText>
      </FormGroup>
      <FormGroup label="SSH key for the cluster nodes" fieldId="bp-ssh-public-key">
        <HelperText>
          <HelperTextItem>
            Create a key if you do not already have one. You can also paste a public key. The install uses the public key. Download the private key and keep it somewhere else.
          </HelperTextItem>
        </HelperText>
        {status.sshPublicKey && (
          <TextArea
            id="bp-ssh-public-key-stored"
            className="install-secret-key"
            value={status.sshPublicKey}
            readOnly
            aria-label="Public key for this cluster"
            rows={3}
            spellCheck={false}
          />
        )}
        <div className="install-secret-actions">
          <Button id="bp-ssh-create" variant="secondary" onClick={createSshKey} isDisabled={!canSave}>
            Create an SSH key
          </Button>
          <Button id="bp-ssh-download" variant="secondary" onClick={downloadPrivateKey} isDisabled={!canSave || !status.privateKeyStored}>
            Download private key
          </Button>
        </div>
        {!status.privateKeyStored && status.sshPublicKey && (
          <HelperText>
            <HelperTextItem>No private key is stored here. Keep the private half wherever you created this public key.</HelperTextItem>
          </HelperText>
        )}
        <TextArea
          id="bp-ssh-public-key"
          style={{ resize: 'vertical', minHeight: '60px', fontFamily: 'monospace', fontSize: '0.85rem' }}
          value={publicKeyText}
          onChange={(_event, value) => setPublicKeyText(value)}
          placeholder="ssh-ed25519 AAAA... comment"
          spellCheck={false}
          autoComplete="off"
          aria-label="Paste an existing public key"
        />
        <div className="install-secret-actions">
          <Button id="bp-ssh-public-key-save" variant="secondary" onClick={usePublicKey} isDisabled={!canSave}>
            Use this public key
          </Button>
        </div>
      </FormGroup>
      {!httpsEnabled && (
        <p role="status">Open this page over HTTPS before saving a pull secret or SSH key.</p>
      )}
      {!outputDirectory && (
        <p role="status">Choose the workspace directory before saving a pull secret or SSH key.</p>
      )}
      {(error || message) && (
        <p id="bp-install-secret-status" role={error ? 'alert' : 'status'}>{error || message}</p>
      )}
    </div>
  );
}

function BlueprintStep({ blueprint, onChangeField, onChangeListField, onAddRegistry, onRegistryChange, onRemoveRegistry, outputDirectory, onOutputDirectoryChange, ansibleWorkingDir, httpsEnabled }) {
  return (
    <PageSection variant="default" className="workflow-page">
      <div className="workflow-page-content">
      <div className="pf-v6-u-mb-md">
        <div className="workflow-page-header">
          <div>
            <span className="workflow-eyebrow">CLUSTER FOUNDATION</span>
            <Title headingLevel="h1" size="2xl" className="pf-v6-u-mb-xs"><ClusterIcon className="pf-v6-u-mr-sm" style={{ color: '#ee0000' }} /> Global Cluster Blueprint</Title>
            <p>Set the shared identity, topology, registry, and trust settings for this installation.</p>
          </div>
        </div>
      </div>
      <Gallery hasGutter minWidths={{ default: 'min(100%, 430px)' }}>
        {/* Card A: Identity Info */}
        <GalleryItem>
          <Card isPlain className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h2" size="lg">Identity & Topology</Title></CardHeader>
            <CardBody>
              <FormGroup label="Cluster Name" isRequired fieldId="bp-cluster-name"><TextInput id="bp-cluster-name" value={blueprint.clusterName} onChange={(_e, val) => onChangeField('clusterName', val)} /></FormGroup>
              <FormGroup label="Base Domain" isRequired fieldId="bp-base-domain" className="pf-v6-u-mt-md"><TextInput id="bp-base-domain" value={blueprint.baseDomain} onChange={(_e, val) => onChangeField('baseDomain', val)} placeholder="ocp.internal" /></FormGroup>
              <FormGroup label="Topology" className="pf-v6-u-mt-md">
                <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
                  <Radio id="topo-compact" name="t-g" label="Compact" isChecked={blueprint.controlPlaneTopology === 'Compact'} onChange={() => onChangeField('controlPlaneTopology', 'Compact')} />
                  <Radio id="topo-ha" name="t-g" label="Highly Available" isChecked={blueprint.controlPlaneTopology === 'HighlyAvailable'} onChange={() => onChangeField('controlPlaneTopology', 'HighlyAvailable')} />
                  <Radio id="topo-sno" name="t-g" label="Single node" isChecked={blueprint.controlPlaneTopology === 'SingleNode'} onChange={() => onChangeField('controlPlaneTopology', 'SingleNode')} />
                </div>
                <HelperText><HelperTextItem>Compact is three nodes, and each node is a master, a worker, and storage. Larger clusters can mix those roles.</HelperTextItem></HelperText>
              </FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
        <GalleryItem>
          <Card isPlain className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h2" size="lg">Cluster Network Services</Title></CardHeader>
            <CardBody>
              <FormGroup
                label="Machine network CIDR"
                fieldId="bp-machine-network-cidr"
                labelHelp={<span>Subnet where the OpenShift hosts are connected, for example 192.0.2.0/24.</span>}
              >
                <TextInput
                  id="bp-machine-network-cidr"
                  value={blueprint.machineNetworkCidr || ''}
                  onChange={(_event, value) => onChangeField('machineNetworkCidr', value)}
                  placeholder="192.0.2.0/24"
                />
              </FormGroup>
              <FormGroup label="Network gateway" fieldId="bp-network-gateway" className="pf-v6-u-mt-md">
                <TextInput id="bp-network-gateway" value={blueprint.networkGateway || ''} onChange={(_event, value) => onChangeField('networkGateway', value)} placeholder="192.0.2.1" />
                <HelperText><HelperTextItem>Written as networkGateway. The DHCP pool is not stored here.</HelperTextItem></HelperText>
              </FormGroup>
              <FormGroup label="API VIP" fieldId="bp-api-vip" className="pf-v6-u-mt-md">
                <TextInput id="bp-api-vip" value={blueprint.apiVip || ''} onChange={(_event, value) => onChangeField('apiVip', value)} placeholder="192.0.2.2" />
                <HelperText><HelperTextItem>Written as apiVip. Compact and Highly Available need this address. Single node leaves it blank.</HelperTextItem></HelperText>
              </FormGroup>
              <FormGroup label="Ingress VIP" fieldId="bp-ingress-vip" className="pf-v6-u-mt-md">
                <TextInput id="bp-ingress-vip" value={blueprint.ingressVip || ''} onChange={(_event, value) => onChangeField('ingressVip', value)} placeholder="192.0.2.3" />
                <HelperText><HelperTextItem>Written as ingressVip. Use a different machine-network address from the API VIP and from every node.</HelperTextItem></HelperText>
              </FormGroup>
              <FormGroup label="DNS servers" fieldId="bp-dns-servers">
                <TextArea
                  id="bp-dns-servers"
                  value={(blueprint.dnsServers || []).join('\n')}
                  onChange={(_event, value) => onChangeListField('dnsServers', value)}
                  placeholder={'192.0.2.35'}
                  rows={3}
                />
                <HelperText><HelperTextItem>Enter one IP address or hostname per line.</HelperTextItem></HelperText>
              </FormGroup>
              <FormGroup label="NTP sources" fieldId="bp-ntp-servers">
                <TextArea
                  id="bp-ntp-servers"
                  value={(blueprint.ntpServers || []).join('\n')}
                  onChange={(_event, value) => onChangeListField('ntpServers', value)}
                  placeholder={'time1.example.com\ntime2.example.com'}
                  rows={3}
                />
                <HelperText><HelperTextItem>Enter one server IP address or hostname per line.</HelperTextItem></HelperText>
              </FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
        <GalleryItem>
          <Card isPlain className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h2" size="lg">Ansible Workspace</Title></CardHeader>
            <CardBody>
              <p>Choose where generated inventory and variable files will be written. The selected directory must be inside the mounted workspace.</p>
              <FormGroup label="Workspace directory" isRequired fieldId="ansible-output-directory">
                <TextInput
                  id="ansible-output-directory"
                  value={outputDirectory}
                  onChange={(_event, value) => onOutputDirectoryChange(value)}
                />
                <HelperText>
                  <HelperTextItem>
                    Allowed workspace: {ansibleWorkingDir || 'the mounted Ansible workspace'}.
                    Files are written in an ocp-metal-ui subdirectory.
                  </HelperTextItem>
                </HelperText>
              </FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
        {/* Card B: Registry & Disconnected Air-Gap Mirrors */}
        <GalleryItem>
          <Card isPlain className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h2" size="lg">Registry Sync</Title></CardHeader>
            <CardBody>
              <FormGroup label="Air-Gapped Mirror Registries" fieldId="bp-mirrors">
                {blueprint.mirrorRegistries.map((registry, index) => (
                  <div key={index} className="blueprint-registry-row">
                    <TextInput
                      id={`bp-mirror-${index}`}
                      placeholder="registry.example.com:5000"
                      value={registry}
                      onChange={(_event, value) => onRegistryChange(index, value)}
                    />
                    <Button variant="danger" onClick={() => onRemoveRegistry(index)}>Remove</Button>
                  </div>
                ))}
                <Button variant="secondary" onClick={onAddRegistry} className="pf-v6-u-mt-xs">
                  <AddCircleOIcon /> Add mirror source
                </Button>
              </FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
        {/* Card C: non-secret CA trust configuration */}
        <GalleryItem>
          <Card isPlain className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h2" size="lg"><KeyIcon className="pf-v6-u-mr-xs" style={{ color: '#ee0000' }} /> Enterprise Security & CA Trust</Title></CardHeader>
            <CardBody>
              <FormGroup label="Additional Trust Bundle (Root CA Certificate PEM)" fieldId="bp-ca-certs">
                <TextArea style={{ resize: 'vertical', minHeight: '80px', fontFamily: 'monospace', fontSize: '0.85rem' }} id="bp-ca-certs" value={blueprint.caCertBundle} onChange={(_e, val) => onChangeField('caCertBundle', val)} placeholder="-----BEGIN CERTIFICATE-----\nMIID5TCCAs2gAwIBAgIJAK...\n-----END CERTIFICATE-----" />
              </FormGroup>
              <InstallSecrets outputDirectory={outputDirectory} httpsEnabled={httpsEnabled} />
              <p>BMC passwords are entered only when inventory is retrieved, and they are not stored. The pull secret and the SSH key are not written to inventory, group_vars, host_vars, or the database.</p>
            </CardBody>
          </Card>
        </GalleryItem>
      </Gallery>
      </div>
    </PageSection>
  );
}

function RedfishManagementStep({
  endpoints,
  httpsEnabled,
  onAddEndpoint,
  onEndpointChange,
  onRemoveEndpoint,
  onSaveEndpoints,
  onRetrieve,
  onReviewNodes
}) {
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');

  const saveEndpoints = async () => {
    setError('');
    setStatus('');
    try {
      await onSaveEndpoints();
      setStatus('Redfish BMC profiles saved in PostgreSQL.');
    } catch (saveError) {
      setError(saveError.message);
    }
  };

  return (
    <PageSection variant="default" className="workflow-page">
      <div className="workflow-page-content">
        <div className="workflow-page-header">
          <div>
            <span className="workflow-eyebrow">OUT-OF-BAND DISCOVERY</span>
            <Title headingLevel="h1" size="2xl">BMC / iLO Profiles</Title>
            <p>Retrieve server inventory securely when PXE or IPA discovery is unavailable.</p>
          </div>
          <Button variant="secondary" onClick={onAddEndpoint} icon={<AddCircleOIcon />}>Add BMC profile</Button>
        </div>
      <Card isPlain className="openshift-node-card">
        <CardHeader style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Title headingLevel="h2" size="lg">Redfish endpoints</Title>
        </CardHeader>
        <CardBody>
          <p>Use this workflow when PXE/IPA is unavailable. Profiles contain only a name and BMC URL. Retrieved hardware joins the shared discovered-node workflow for inventory assignment and hardware configuration. Credentials are requested only for retrieval and are never saved.</p>
          {!httpsEnabled && (
            <p role="alert" style={{ color: '#a30000', fontWeight: 600 }}>
              HTTPS is not configured. BMC retrieval is disabled because credentials must not be sent over HTTP.
            </p>
          )}
          {(error || status) && <p role={error ? 'alert' : 'status'} className={`workflow-status${error ? ' is-error' : ''}`}>{error || status}</p>}
          {!endpoints.length && (
            <div className="bmc-empty-state">
              <InfrastructureIcon aria-hidden="true" />
              <strong>No BMC profiles yet</strong>
              <span>Add a Redfish HTTPS endpoint to retrieve hardware inventory from a server.</span>
            </div>
          )}
          {endpoints.map((endpoint, index) => (
            <RedfishEndpointCard
              key={endpoint.id}
              endpoint={endpoint}
              index={index}
              httpsEnabled={httpsEnabled}
              onChange={onEndpointChange}
              onRemove={onRemoveEndpoint}
              onRetrieve={onRetrieve}
            />
          ))}
          <div className="bmc-actions">
            <Button variant="secondary" onClick={saveEndpoints} isDisabled={endpoints.length === 0}>Save BMC profiles</Button>
            <Button variant="primary" onClick={onReviewNodes}>Continue to Discovered Nodes</Button>
          </div>
        </CardBody>
      </Card>
      </div>
    </PageSection>
  );
}

function RedfishEndpointCard({ endpoint, index, httpsEnabled, onChange, onRemove, onRetrieve }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [allowUntrustedTls, setAllowUntrustedTls] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);

  const retrieve = async () => {
    setError('');
    setStatus('');
    setBusy(true);
    try {
      const result = await onRetrieve(endpoint.id, username, password, allowUntrustedTls);
      const warnings = result.warnings || [];
      setStatus(
        `Retrieved ${result.serial_number || result.uuid}${warnings.length ? `. Warnings: ${warnings.join(' ')}` : '.'}`
      );
    } catch (retrieveError) {
      setError(retrieveError.message);
    } finally {
      setPassword('');
      setUsername('');
      setBusy(false);
    }
  };

  return (
    <Card isPlain className="openshift-node-card" style={{ marginTop: '16px' }}>
      <CardHeader style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Title headingLevel="h4" size="md">BMC profile {index + 1}</Title>
        <Button variant="plain" aria-label={`Remove BMC profile ${index + 1}`} onClick={() => onRemove(endpoint.id)} style={{ color: '#a30000' }}><TrashIcon /></Button>
      </CardHeader>
      <CardBody>
        <Gallery hasGutter minWidths={{ default: '220px' }}>
          <GalleryItem><FormGroup label="Profile name" isRequired fieldId={`redfish-name-${endpoint.id}`}><TextInput id={`redfish-name-${endpoint.id}`} value={endpoint.name} onChange={(_event, value) => onChange(endpoint.id, 'name', value)} /></FormGroup></GalleryItem>
          <GalleryItem>
            <FormGroup label="Redfish HTTPS URL" isRequired fieldId={`redfish-url-${endpoint.id}`}>
              <TextInput id={`redfish-url-${endpoint.id}`} value={endpoint.baseUrl} onChange={(_event, value) => onChange(endpoint.id, 'baseUrl', value)} />
              <HelperText><HelperTextItem>For example: https://192.0.2.20</HelperTextItem></HelperText>
            </FormGroup>
          </GalleryItem>
        </Gallery>
        <Gallery hasGutter minWidths={{ default: '220px' }} style={{ marginTop: '16px' }}>
          <GalleryItem><FormGroup label="BMC username" isRequired fieldId={`redfish-user-${endpoint.id}`}><TextInput id={`redfish-user-${endpoint.id}`} autoComplete="off" value={username} onChange={(_event, value) => setUsername(value)} /></FormGroup></GalleryItem>
          <GalleryItem><FormGroup label="BMC password" isRequired fieldId={`redfish-password-${endpoint.id}`}><TextInput id={`redfish-password-${endpoint.id}`} type="password" autoComplete="new-password" value={password} onChange={(_event, value) => setPassword(value)} /></FormGroup></GalleryItem>
        </Gallery>
        <Checkbox
          id={`redfish-untrusted-${endpoint.id}`}
          label="Accept the risk and proceed if the BMC TLS certificate is untrusted"
          isChecked={allowUntrustedTls}
          onChange={(_event, checked) => setAllowUntrustedTls(checked)}
          className="pf-v6-u-mt-md"
        />
        <div style={{ display: 'flex', gap: '12px', alignItems: 'center', marginTop: '12px' }}>
          <Button variant="secondary" onClick={retrieve} isDisabled={!httpsEnabled || busy || !username || !password}>
            {busy ? 'Retrieving inventory...' : 'Retrieve Redfish inventory'}
          </Button>
          <span role={error ? 'alert' : 'status'} style={{ color: error ? '#a30000' : '#4f5255' }}>{error || status}</span>
        </div>
      </CardBody>
    </Card>
  );
}
function DiscoveredNodesPage({ nodes, assignments, onAssignmentChange, inventoryItems, onDeleteNode, onAddInventoryItem, newNodeName, onNewNodeNameChange, onRefresh, onUpdateInventory, onBack, isRefreshing, status, error }) {
  const discoveredNodes = nodes.filter((node) => !node.is_placeholder);
  const availableInventoryItems = inventoryItems;
  const selectedInventoryItems = Object.entries(assignments)
    .filter(([, assignment]) => assignment.placeholderUuid)
    .map(([nodeUuid, assignment]) => ({ nodeUuid, uuid: assignment.placeholderUuid }));
  const changedAssignments = Object.entries(assignments).flatMap(([nodeUuid, assignment]) => {
    if (assignment.unlink) return [{ nodeUuid, unlink: true }];
    if (assignment.placeholderUuid) return [{ nodeUuid, placeholderUuid: assignment.placeholderUuid }];
    if (assignment.ansibleHost?.trim()) return [{ nodeUuid, ansibleHost: assignment.ansibleHost.trim() }];
    return [];
  });
  const updatedNodeUuids = new Set(changedAssignments.map(({ nodeUuid }) => nodeUuid));
  const consumedPlaceholderUuids = new Set(
    changedAssignments.flatMap(({ placeholderUuid }) => placeholderUuid ? [placeholderUuid] : [])
  );
  const occupiedHosts = nodes
    .filter((node) => !updatedNodeUuids.has(node.uuid) && !consumedPlaceholderUuids.has(node.uuid))
    .map((node) => node.preferences?.ansibleHost)
    .filter(Boolean);
  const draftHosts = changedAssignments.flatMap((assignment) => {
    if (assignment.ansibleHost) return [assignment.ansibleHost];
    if (!assignment.placeholderUuid) return [];
    const item = inventoryItems.find(({ uuid }) => uuid === assignment.placeholderUuid);
    return item?.preferences?.ansibleHost ? [item.preferences.ansibleHost] : [];
  });
  const hasDuplicateDraftHosts = draftHosts.some(
    (host, index) => occupiedHosts.includes(host) || draftHosts.indexOf(host) !== index
  );
  const unlinkedNodeCount = discoveredNodes.filter((node) => {
    const assignment = assignments[node.uuid];
    return assignment ? Boolean(assignment.unlink) : !node.preferences?.ansibleHost;
  }).length;

  return (
    <PageSection variant="default" className="workflow-page">
      <div className="workflow-page-content discovered-page-content">
        <div className="workflow-page-header discovered-page-header">
          <div>
            <span className="workflow-eyebrow">HARDWARE DISCOVERY</span>
            <Title headingLevel="h1" size="2xl">Discovered Nodes</Title>
            <p>Link discovered hardware to inventory names, unlink existing matches, or remove mistaken discoveries.</p>
          </div>
          <div className="discovery-summary">
            <strong aria-live="polite">{discoveredNodes.length} discovered</strong>
            <span>{unlinkedNodeCount} unlinked</span>
            <span>{availableInventoryItems.length} inventory names available</span>
            <Button variant="secondary" onClick={onRefresh} isDisabled={isRefreshing}>
              {isRefreshing ? 'Refreshing...' : 'Refresh inventory'}
            </Button>
          </div>
        </div>

        <div className="add-inventory-row">
          <FormGroup label="Add an inventory name" fieldId="new-ansible-host" isRequired style={{ flex: 1 }}>
            <TextInput id="new-ansible-host" value={newNodeName} onChange={(_event, value) => onNewNodeNameChange(value)} placeholder="control-01" />
          </FormGroup>
          <Button variant="secondary" onClick={onAddInventoryItem} isDisabled={!newNodeName.trim()} icon={<AddCircleOIcon />}>
            Add inventory name
          </Button>
        </div>

        {(error || status) && <p role={error ? 'alert' : 'status'} className={`workflow-status${error ? ' is-error' : ''}`}>{error || status}</p>}
        {hasDuplicateDraftHosts && (
          <p role="alert" className="workflow-status is-error">
            Each inventory name can be assigned only once. Resolve the duplicate selections before saving.
          </p>
        )}

        {discoveredNodes.length || availableInventoryItems.length ? (
          <div style={{ overflowX: 'auto', backgroundColor: '#fff', border: '1px solid #d2d2d2', borderRadius: '4px' }}>
            <table className="workflow-discovery-table">
              <thead>
                <tr>
                  <th>Node or inventory name</th>
                  <th>Link</th>
                  <th aria-label="Actions"></th>
                </tr>
              </thead>
              <tbody>
                {discoveredNodes.map((node) => {
                  const currentHost = node.preferences?.ansibleHost;
                  const assignment = assignments[node.uuid] || {};
                  const hasDraft = Boolean(assignments[node.uuid]);
                  return (
                    <tr key={node.uuid}>
                      <td>
                        <strong>{node.serial_number || 'Serial unavailable'}</strong>
                        {node.source === 'redfish' && <div>{node.bmc_address || 'BMC address unavailable'}</div>}
                        <small style={{ color: '#6a6e73' }}>{node.source === 'redfish' ? 'Redfish' : 'IPA'} · UUID {node.uuid}</small>
                      </td>
                      <td className="assignment-cell">
                        {currentHost && !hasDraft ? (
                          <div className="node-link-state">
                            <strong>Linked as {currentHost}</strong>
                            <Button variant="secondary" onClick={() => onAssignmentChange(node.uuid, 'unlink')}>Unlink</Button>
                          </div>
                        ) : (
                          <div className="node-link-controls">
                            <FormGroup label="Select available inventory name" fieldId={`assignment-${node.uuid}`}>
                              <FormSelect
                                id={`assignment-${node.uuid}`}
                                value={assignment.placeholderUuid || ''}
                                onChange={(_event, value) => value
                                  ? onAssignmentChange(node.uuid, 'placeholderUuid', value)
                                  : onAssignmentChange(node.uuid, 'clear')}
                              >
                                <FormSelectOption value="" label="-- Select an inventory item --" />
                                {inventoryItems.map((item) => {
                                  const selectedByOtherNode = selectedInventoryItems.some(
                                    (selected) => selected.uuid === item.uuid && selected.nodeUuid !== node.uuid
                                  );
                                  return (
                                    <FormSelectOption
                                      key={item.uuid}
                                      value={item.uuid}
                                      label={item.preferences.ansibleHost}
                                      isDisabled={selectedByOtherNode}
                                    />
                                  );
                                })}
                              </FormSelect>
                            </FormGroup>
                            <FormGroup label="Or enter a new inventory name" fieldId={`new-host-${node.uuid}`}>
                              <TextInput
                                id={`new-host-${node.uuid}`}
                                value={assignment.ansibleHost || ''}
                                onChange={(_event, value) => onAssignmentChange(node.uuid, 'ansibleHost', value)}
                                placeholder="worker-01"
                              />
                            </FormGroup>
                            {assignment.unlink && (
                              <Button variant="secondary" onClick={() => onAssignmentChange(node.uuid, 'clear')}>
                                Keep linked as {currentHost}
                              </Button>
                            )}
                            {(assignment.placeholderUuid || assignment.ansibleHost) && (
                              <Button variant="link" onClick={() => onAssignmentChange(node.uuid, 'clear')}>
                                Clear draft link
                              </Button>
                            )}
                          </div>
                        )}
                      </td>
                      <td className="node-action-cell">
                        <Button variant="plain" aria-label={`Delete discovered node ${node.serial_number || node.uuid}`} onClick={() => onDeleteNode(node)} style={{ color: '#a30000' }}>
                          <TrashIcon />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
                {availableInventoryItems.map((item) => {
                  const linkedNode = selectedInventoryItems.find(({ uuid }) => uuid === item.uuid);
                  const availableNodes = discoveredNodes.filter((node) => {
                    const nodeAssignment = assignments[node.uuid];
                    if (node.uuid === linkedNode?.nodeUuid) return true;
                    if (nodeAssignment && !nodeAssignment.unlink) return false;
                    return !node.preferences?.ansibleHost || Boolean(nodeAssignment?.unlink);
                  });
                  return (
                    <tr key={item.uuid}>
                      <td>
                        <strong>{item.preferences?.ansibleHost}</strong>
                        <div>Inventory name · awaiting discovered hardware</div>
                      </td>
                      <td className="assignment-cell">
                        <FormGroup label="Select an available discovered node" fieldId={`inventory-link-${item.uuid}`}>
                          <FormSelect
                            id={`inventory-link-${item.uuid}`}
                            value={linkedNode?.nodeUuid || ''}
                            onChange={(_event, value) => {
                              if (linkedNode && linkedNode.nodeUuid !== value) {
                                onAssignmentChange(linkedNode.nodeUuid, 'clear');
                              }
                              if (value) onAssignmentChange(value, 'placeholderUuid', item.uuid);
                            }}
                          >
                            <FormSelectOption value="" label="-- Select a discovered node --" />
                            {availableNodes.map((node) => (
                              <FormSelectOption
                                key={node.uuid}
                                value={node.uuid}
                                label={`${node.serial_number || node.uuid} · ${node.source === 'redfish' ? 'Redfish' : 'IPA'}`}
                              />
                            ))}
                          </FormSelect>
                        </FormGroup>
                      </td>
                      <td className="node-action-cell">
                        <Button variant="plain" aria-label={`Delete inventory name ${item.preferences?.ansibleHost}`} onClick={() => onDeleteNode(item)} style={{ color: '#a30000' }}>
                          <TrashIcon />
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Card isPlain className="openshift-node-card">
            <CardBody><p>No discovered nodes or unlinked inventory names. Refresh after IPA reports arrive, or add inventory names above.</p></CardBody>
          </Card>
        )}

        <div className="workflow-actions pf-v6-u-mt-xl">
          <Button variant="secondary" onClick={onBack}>Back to inventory configuration</Button>
          <Button variant="primary" onClick={onUpdateInventory} isDisabled={!changedAssignments.length || hasDuplicateDraftHosts || isRefreshing}>
            Save links
          </Button>
        </div>
      </div>
    </PageSection>
  );
}

function InventoryStep({ nodes, configs, onChangeField, onBondToggle, onReview, onCommitConfiguration, isSavingConfiguration, status, error }) {
  const inventoryNodes = nodes.filter((node) => !node.is_placeholder && node.preferences?.ansibleHost);

  return (
    <PageSection variant="default" className="workflow-page">
      <div className="pf-v6-u-mb-md">
        <div className="workflow-page-header">
          <div>
            <span className="workflow-eyebrow">NODE PREFERENCES</span>
            <Title headingLevel="h1" size="2xl" className="pf-v6-u-mb-xs"><InfrastructureIcon className="pf-v6-u-mr-sm" style={{ color: '#ee0000' }} /> Inventory Configuration</Title>
            <p>Choose installation disks, network interfaces, bonds, and VLANs for each assigned node.</p>
          </div>
        </div>
        {(error || status) && <p role={error ? 'alert' : 'status'} className={`workflow-status${error ? ' is-error' : ''}`}>{error || status}</p>}
      </div>
      {inventoryNodes.length === 0 && !error && (
        <Card isPlain className="openshift-node-card">
          <CardBody>
            <p>No discovered nodes have been assigned to inventory yet. Use Discovered Nodes to refresh hardware and assign inventory host names.</p>
          </CardBody>
        </Card>
      )}
      <Gallery hasGutter minWidths={{ default: 'min(100%, 680px)' }}>
        {inventoryNodes.map(node => {
          const cfg = configs[node.uuid] || {};
          const selectedHardware = node;
          const hasHardware = true;
          return (
            <GalleryItem key={node.uuid}>
              <Card isPlain className="openshift-node-card">
                <CardHeader style={{ paddingBottom: '8px' }}>
                  <div>
                    <Title headingLevel="h3" size="lg" style={{ color: '#151515' }}>
                      <strong>
                        Node Host: {selectedHardware.serial_number || 'Serial unavailable'} ({cfg.ansibleHost || 'Unnamed host'})
                      </strong>
                    </Title>
                    <small style={{ color: '#6a6e73', fontFamily: 'monospace' }}>UUID: {selectedHardware.uuid}</small>
                  </div>
                  <span style={{ color: '#8a0000', marginLeft: 'auto', fontSize: '0.85rem', fontWeight: '600', backgroundColor: '#ffe6e6', padding: '2px 8px', borderRadius: '2px' }}>
                    {hasHardware
                      ? <><CheckCircleIcon className="pf-v6-u-mr-xs" /> {selectedHardware.source === 'redfish' ? 'Redfish Inventory' : 'IPA Inspection'} Received</>
                      : 'Waiting for IPA hardware'}
                  </span>
                </CardHeader>
                <CardBody style={{ paddingTop: '8px' }}>
                  <div className="pf-v6-u-mb-md" style={{ backgroundColor: '#f5f5f5', padding: '8px 10px', borderRadius: '4px', fontSize: '0.8rem', borderLeft: '3px solid #c9190b' }}>
                    <strong>Hardware Profile Data:</strong>
                    {hasHardware ? (
                      <div>
                        <div>{selectedHardware.inventory.cpus.count} Cores ({selectedHardware.inventory.cpus.model})</div>
                        <div>{Math.round(selectedHardware.inventory.memory.total / 1073741824)} GB RAM Allocated</div>
                        <div className="node-device-lists">
                          <div>
                            <strong>Storage devices ({(selectedHardware.inventory.disks || []).length})</strong>
                            <div className="node-device-list">
                              {(selectedHardware.inventory.disks || []).length === 0 && <div>No storage devices reported</div>}
                              {(selectedHardware.inventory.disks || []).map((disk) => (
                                <div key={disk.serial}>{disk.model} — {disk.name || disk.serial} ({Math.round(disk.size / 1000000000)} GB)</div>
                              ))}
                            </div>
                          </div>
                          <div>
                            <strong>NICs ({(selectedHardware.inventory.interfaces || []).length})</strong>
                            <div className="node-device-list">
                              {(selectedHardware.inventory.interfaces || []).length === 0 && <div>No NICs reported</div>}
                              {(selectedHardware.inventory.interfaces || []).map((nic) => (
                                <div key={nic.name}>{nic.name} [{nic.mac}]</div>
                              ))}
                            </div>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div>Select an IPA node to view hardware</div>
                    )}
                  </div>
                  <FormGroup label="Install disk" isRequired fieldId={`disk-${node.uuid}`} className="pf-v6-u-mb-md">
                    <FormSelect id={`disk-${node.uuid}`} value={cfg.rootDisk || ''} onChange={(_e, val) => onChangeField(node.uuid, 'rootDisk', val)} isDisabled={!hasHardware}>
                      <FormSelectOption value="" label="-- Choose target drive --" isDisabled />
                      {(selectedHardware?.inventory.disks || []).map(d => {
                        const speedTag = d.model.toLowerCase().includes('nvme') ? ' [Fastest: NVMe]' : d.model.toLowerCase().includes('ssd') ? ' [Fast: SSD]' : '';
                        return <FormSelectOption key={d.serial} value={d.serial} label={`${d.model}${speedTag} (${Math.round(d.size / 1000000000)} GB)`} />;
                      })}
                    </FormSelect>
                    <HelperText><HelperTextItem>OpenShift installs on this disk. The other reported drives stay in the host inventory.</HelperTextItem></HelperText>
                  </FormGroup>
                  <Title headingLevel="h4" size="md" className="pf-v6-u-mb-xs" style={{ display: 'flex', alignItems: 'center', color: '#151515' }}><NetworkIcon className="pf-v6-u-mr-xs" style={{ color: '#4f5255' }} /> Logical Interface Mapping</Title>
                  <div className="pf-v6-u-mb-sm" style={{ display: 'flex', gap: '20px' }}>
                    <Radio id={`net-single-${node.uuid}`} name={`net-${node.uuid}`} label="Standalone NIC" isChecked={cfg.netType === 'single'} onChange={() => onChangeField(node.uuid, 'netType', 'single')} isDisabled={!hasHardware} />
                    <Radio id={`net-bond-${node.uuid}`} name={`net-${node.uuid}`} label="Aggregated Bond" isChecked={cfg.netType === 'bond'} onChange={() => onChangeField(node.uuid, 'netType', 'bond')} isDisabled={!hasHardware} />
                  </div>
                  {cfg.netType === 'single' && (
                    <div style={{ border: '1px solid #d2d2d2', padding: '12px', borderRadius: '4px', backgroundColor: '#fafafa' }}>
                      <FormGroup label="Machine-network NIC" isRequired fieldId={`s-nic-${node.uuid}`} className="pf-v6-u-mb-sm">
                        <FormSelect
                          id={`s-nic-${node.uuid}`}
                          value={cfg.singleNic || ''}
                          onChange={(_event, value) => onChangeField(node.uuid, 'singleNic', value)}
                          isDisabled={!hasHardware}
                        >
                          <FormSelectOption value="" label="-- Choose port --" isDisabled />
                          {(selectedHardware?.inventory.interfaces || []).map((nic) => {
                            const linkIndicator = nic.linkStatus === 'up'
                              ? '🟩 Connected'
                              : nic.linkStatus === 'down'
                                ? '🟥 Disconnected'
                                : '⬜ Unknown';
                            return (
                              <FormSelectOption
                                key={nic.name}
                                value={nic.name}
                                label={`${linkIndicator} — ${nic.name} [${nic.mac}]`}
                              />
                            );
                          })}
                        </FormSelect>
                        <HelperText><HelperTextItem>The other reported NICs stay in the host inventory.</HelperTextItem></HelperText>
                      </FormGroup>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }} className="pf-v6-u-mt-sm"><Checkbox label="Enable VLAN Encapsulation" id={`v-s-${node.uuid}`} isChecked={cfg.singleVlanEnabled || false} onChange={(_e, chk) => onChangeField(node.uuid, 'singleVlanEnabled', chk)} isDisabled={!hasHardware} />{cfg.singleVlanEnabled && <TextInput style={{ maxWidth: '90px', height: '26px', fontSize: '0.85rem' }} type="number" placeholder="VLAN ID" value={cfg.singleVlanId || ''} onChange={(_e, val) => onChangeField(node.uuid, 'singleVlanId', val)} />}</div>
                    </div>
                  )}
                  {cfg.netType === 'bond' && (
                    <div style={{ border: '1px solid #d2d2d2', padding: '12px', borderRadius: '4px', backgroundColor: '#f4f6f9' }}>
                      <div style={{ display: 'grid', gap: '4px' }}>
                        {(selectedHardware?.inventory.interfaces || []).map((nic) => (
                          <div key={nic.name} style={{ padding: '6px 8px', border: '1px solid #d2d2d2', borderRadius: '3px', backgroundColor: '#fff' }}>
                            <Checkbox
                              label={(
                                <span>
                                  <NicLinkIndicator status={nic.linkStatus} />
                                  {nic.name} [{nic.mac}] — {nic.linkStatus === 'up' ? 'Connected' : nic.linkStatus === 'down' ? 'Disconnected' : 'Unknown'}
                                </span>
                              )}
                              id={`b-n-${node.uuid}-${nic.name}`}
                              isChecked={(cfg.bondNics || []).includes(nic.name)}
                              onChange={(_e, checked) => onBondToggle(node.uuid, nic.name, checked)}
                              isDisabled={!hasHardware}
                            />
                          </div>
                        ))}
                      </div>
                      <HelperText><HelperTextItem>Checked NICs form the machine-network bond. Unchecked NICs stay in the host inventory.</HelperTextItem></HelperText>
                      <div style={{ borderTop: '1px dashed #c5c5c5' }} className="pf-v6-u-mt-sm pf-v6-u-pt-sm"><div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}><Checkbox label="Attach VLAN to bond0" id={`v-b-${node.uuid}`} isChecked={cfg.bondVlanEnabled || false} onChange={(_e, chk) => onChangeField(node.uuid, 'bondVlanEnabled', chk)} isDisabled={!hasHardware} />{cfg.bondVlanEnabled && <TextInput style={{ maxWidth: '90px', height: '26px', fontSize: '0.85rem' }} type="number" placeholder="VLAN ID" value={cfg.bondVlanId || ''} onChange={(_e, val) => onChangeField(node.uuid, 'bondVlanId', val)} />}</div></div>
                    </div>
                  )}
                  <FormGroup label="Roles" fieldId={`role-${node.uuid}`} className="pf-v6-u-mt-md">
                    <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
                      {NODE_ROLES.map((role) => (
                        <Checkbox
                          key={role}
                          id={`role-${role}-${node.uuid}`}
                          label={ROLE_LABELS[role]}
                          isChecked={selectedRoles(cfg).includes(role)}
                          onChange={(_event, checked) => {
                            const nextRoles = NODE_ROLES.filter((name) => (name === role ? checked : selectedRoles(cfg).includes(name)));
                            onChangeField(node.uuid, 'roles', nextRoles);
                          }}
                          isDisabled={!hasHardware}
                        />
                      ))}
                    </div>
                    <HelperText><HelperTextItem>Written as roles in host_vars. Compact checks all three. A master is the installer role when it is selected with other duties.</HelperTextItem></HelperText>
                  </FormGroup>
                  <Checkbox
                    className="pf-v6-u-mt-sm"
                    label="Rendezvous host"
                    id={`rendezvous-${node.uuid}`}
                    isChecked={cfg.rendezvous === true}
                    onChange={(_event, checked) => onChangeField(node.uuid, 'rendezvous', checked)}
                    isDisabled={!hasHardware || !selectedRoles(cfg).includes('master')}
                  />
                  <HelperText><HelperTextItem>Written as rendezvous. One master holds the rendezvous IP.</HelperTextItem></HelperText>
                  <FormGroup label="Last octet" fieldId={`octet-${node.uuid}`} className="pf-v6-u-mt-md">
                    <TextInput
                      id={`octet-${node.uuid}`}
                      value={cfg.ipv4LastOctet || ''}
                      onChange={(_event, value) => onChangeField(node.uuid, 'ipv4LastOctet', value)}
                      placeholder="15"
                      isDisabled={!hasHardware}
                    />
                    <HelperText>
                      <HelperTextItem>Written as ipv4LastOctet in host_vars. Combined with the machine network. Leave blank to keep the IPA address.</HelperTextItem>
                    </HelperText>
                  </FormGroup>
                </CardBody>
              </Card>
            </GalleryItem>
          );
        })}
      </Gallery>
      <div className="workflow-actions">
        <div>
          <Button variant="secondary" onClick={onCommitConfiguration} isDisabled={isSavingConfiguration}>
            {isSavingConfiguration ? 'Committing...' : 'Commit Configuration'}
          </Button>
          <p role={error ? 'alert' : 'status'} className="workflow-action-status">{error || status}</p>
        </div>
        <Button variant="primary" onClick={onReview}>
          Continue to Review
        </Button>
      </div>
    </PageSection>
  );
}

function InventoryReviewPage({ nodes, configs, machineNetworkCidr, outputDirectory, status, error, isRefreshing, onRefresh, onBack, onConfirm, configPreview, onViewYaml }) {
  const discoveredNodes = nodes.filter((node) => !node.is_placeholder);
  const unnamedNodes = nodes.filter((node) => !configs[node.uuid]?.ansibleHost?.trim());
  const missingSerials = discoveredNodes.filter((node) => !node.serial_number);
  const isCommitting = status.startsWith('Committing');
  const canConfirm = nodes.length > 0
    && Boolean(outputDirectory)
    && unnamedNodes.length === 0
    && missingSerials.length === 0
    && !isCommitting;

  return (
    <PageSection variant="default" className="workflow-page">
      <div style={{ maxWidth: '1100px', margin: '0 auto' }}>
        <div className="workflow-page-header">
          <div>
            <span className="workflow-eyebrow">FINAL CHECK</span>
            <Title headingLevel="h1" size="2xl">Review and confirm</Title>
            <p>Check host names and hardware identifiers before generating Ansible files.</p>
          </div>
        </div>
        <div role={error ? 'alert' : 'status'} className="pf-v6-u-mt-md" style={{ color: error ? '#a30000' : '#4f5255' }}>
          {error || status}
        </div>
        <Card isPlain className="openshift-node-card pf-v6-u-mt-md">
          <CardHeader>
            <Title headingLevel="h2" size="lg">Inventory summary</Title>
          </CardHeader>
          <CardBody>
            <p><strong>{nodes.length}</strong> total nodes: <strong>{discoveredNodes.length}</strong> discovered, <strong>{nodes.length - discoveredNodes.length}</strong> named and awaiting hardware.</p>
            <p><strong>Destination:</strong> {outputDirectory || 'Not configured'}/ocp-metal-ui/</p>
            {unnamedNodes.length > 0 && <p role="alert" style={{ color: '#a30000' }}>{unnamedNodes.length} node(s) need a host name before continuing.</p>}
            {missingSerials.length > 0 && <p role="alert" style={{ color: '#a30000' }}>{missingSerials.length} discovered node(s) have no serial number; export will be rejected until each serial is reported.</p>}
          </CardBody>
        </Card>
        {nodes.length > 0 ? (
          <Card isPlain className="openshift-node-card pf-v6-u-mt-md">
            <CardHeader>
              <Title headingLevel="h2" size="lg">Node roster</Title>
              <Button variant="secondary" onClick={onRefresh} isDisabled={isRefreshing}>
                {isRefreshing ? 'Refreshing...' : 'Refresh inventory'}
              </Button>
            </CardHeader>
            <CardBody>
              <div role="list" aria-label="All nodes in inventory" className="review-roster">
                {nodes.map((node) => {
                  const config = configs[node.uuid] || {};
                  const statusText = node.is_placeholder
                    ? 'Named host · waiting for hardware'
                    : `${node.source === 'redfish' ? 'Redfish' : 'IPA'} · discovered`;
                  const address = hostAddress(
                    config.ipv4LastOctet,
                    machineNetworkCidr,
                    node.inventory?.current_ip
                  );
                  const vlan = assignedVlan(config);
                  return (
                    <div key={node.uuid} role="listitem" className="review-roster-item">
                      <div><strong>Host name</strong><br />{config.ansibleHost || 'Not assigned'}</div>
                      <div><strong>Roles</strong><br />{roleLabel(config)}</div>
                      <div><strong>Rendezvous</strong><br />{config.rendezvous ? 'Yes' : 'No'}</div>
                      <div><strong>Storage devices</strong><br />{(node.inventory?.disks || []).length}</div>
                      <div><strong>NICs</strong><br />{(node.inventory?.interfaces || []).length}</div>
                      <div><strong>Install disk</strong><br />{config.rootDisk || 'Not chosen'}</div>
                      <div><strong>Machine NIC</strong><br />{config.netType === 'bond' ? (config.bondNics || []).join(', ') || 'Bond not chosen' : config.singleNic || 'Not chosen'}</div>
                      <div><strong>Address</strong><br />{address}</div>
                      {vlan && (
                        <div><strong>VLAN</strong><br />{vlan}</div>
                      )}
                      <div><strong>Serial number</strong><br />{node.serial_number || (node.is_placeholder ? 'Awaiting discovery' : 'Not reported')}</div>
                      {node.source === 'redfish' && (
                        <div><strong>BMC address</strong><br />{node.bmc_address || 'Not reported'}</div>
                      )}
                      <div><strong>Status</strong><br />{statusText}</div>
                    </div>
                  );
                })}
              </div>
            </CardBody>
          </Card>
        ) : (
          <Card isPlain className="openshift-node-card pf-v6-u-mt-md">
            <CardBody><p>No nodes are currently in the inventory. Go back to add a named host or wait for discovery.</p></CardBody>
          </Card>
        )}
        <p className="yaml-review-note">
          {configPreview?.installConfigFile
            ? `Wrote ${configPreview.installConfigFile} and ${configPreview.agentConfigFile}.`
            : (configPreview?.installFilesMessage
              || 'Generate writes the inventory under ocp-metal-ui. A readable host pull secret, plus an SSH public key, also writes install-config.yaml and agent-config.yaml in cluster-install.')}
        </p>
        <div className="workflow-actions pf-v6-u-pt-lg">
          <Button variant="secondary" onClick={onBack}>Back to inventory</Button>
          <div className="yaml-review-actions">
            <Button variant="secondary" onClick={() => onViewYaml('install')} isDisabled={!configPreview?.installConfig}>
              View install-config
            </Button>
            <Button variant="secondary" onClick={() => onViewYaml('agent')} isDisabled={!configPreview?.agentConfig}>
              View agent-config
            </Button>
          </div>
          <Button variant="primary" onClick={onConfirm} isDisabled={!canConfirm} >
            {isCommitting ? 'Generating...' : 'Generate Ansible Files'}
          </Button>
        </div>
        {configPreview?.error && <p role="alert" className="yaml-review-error">{configPreview.error}</p>}
      </div>
    </PageSection>
  );
}

function renderYaml(text) {
  const lines = highlightYamlTokens(text);
  return lines.map((tokens, lineIndex) => (
    <span key={lineIndex}>
      {tokens.map((part, partIndex) => (
        part.kind
          ? <span key={partIndex} className={`yaml-token-${part.kind}`}>{part.text}</span>
          : part.text
      ))}
      {lineIndex < lines.length - 1 ? '\n' : null}
    </span>
  ));
}

function YamlPopout({ which, installConfig, agentConfig, written, onChoose, onClose }) {
  const panelRef = useRef(null);
  useEffect(() => {
    panelRef.current?.focus();
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  const title = which === 'agent' ? 'agent-config.yaml' : 'install-config.yaml';
  const text = which === 'agent' ? agentConfig : installConfig;
  return (
    <div className="yaml-popout" role="presentation" onClick={onClose}>
      <div
        className="yaml-popout-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="yaml-popout-title"
        tabIndex={-1}
        ref={panelRef}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="yaml-popout-toolbar">
          <strong id="yaml-popout-title">{title}</strong>
          <div className="yaml-popout-choice">
            <Button variant={which === 'install' ? 'primary' : 'secondary'} onClick={() => onChoose('install')}>
              install-config.yaml
            </Button>
            <Button variant={which === 'agent' ? 'primary' : 'secondary'} onClick={() => onChoose('agent')}>
              agent-config.yaml
            </Button>
          </div>
          <Button variant="secondary" onClick={onClose}>Close</Button>
        </div>
        {which === 'install' && (
          <p className="yaml-popout-note">
            {written
              ? 'This is the install-config.yaml written in cluster-install. The agent-config.yaml beside it is the matching file.'
              : 'Save an SSH public key and provide the host pull secret file, then generate again.'}
          </p>
        )}
        {which === 'agent' && written && (
          <p className="yaml-popout-note">This is the agent-config.yaml written in cluster-install.</p>
        )}
        <pre className="yaml-popout-body">{renderYaml(text)}</pre>
      </div>
    </div>
  );
}

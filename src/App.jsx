import { useState, useEffect, useCallback } from 'react';

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

// Core stylesheets required for official compilation layout rules
import '@patternfly/react-core/dist/styles/base.css';
import '@patternfly/react-styles/css/components/Wizard/wizard.css';
import '@patternfly/react-styles/css/components/Card/card.css';
import '@patternfly/react-styles/css/components/Page/page.css';
import '@patternfly/react-styles/css/components/Masthead/masthead.css';

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

  const [blueprint, setBlueprint] = useState({});
  const [redfishEndpoints, setRedfishEndpoints] = useState([]);
  const [httpsEnabled, setHttpsEnabled] = useState(false);
  const [isSavingConfiguration, setIsSavingConfiguration] = useState(false);

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
    setInventoryStatus('Saving configuration to SQLite...');
    setIsSavingConfiguration(true);
    try {
      const response = await fetch('/api/v1/configuration', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: nodeConfigs, blueprint, outputDirectory })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Configuration could not be saved (HTTP ${response.status}).`);
      setInventoryStatus(`Configuration saved to SQLite for ${result.savedNodes} node${result.savedNodes === 1 ? '' : 's'}.`);
    } catch (error) {
      setInventoryError(error.message);
      setInventoryStatus('Configuration save failed');
    } finally {
      setIsSavingConfiguration(false);
    }
  };

  const loadInventory = useCallback(async () => {
    const response = await fetch('/api/v1/nodes');
    if (!response.ok) throw new Error(`Inventory API returned HTTP ${response.status}.`);
    const loadedNodes = (await response.json()).map(normalizeNode);
    setNodes(loadedNodes);
    setNodeConfigs((current) => Object.fromEntries(loadedNodes.map((node) => {
      const savedPreferences = Object.fromEntries(
        Object.entries(node.preferences || {}).filter(([key]) => key !== 'ansibleGroup')
      );
      const currentPreferences = Object.fromEntries(
        Object.entries(current[node.uuid] || {}).filter(([key]) => key !== 'ansibleGroup')
      );
      return [node.uuid, {
        rootDisk: findFastestDrive(node.inventory.disks),
        netType: 'single',
        singleNic: node.inventory.interfaces[0]?.name || '',
        singleVlanEnabled: false,
        singleVlanId: '',
        bondNics: [],
        bondVlanEnabled: false,
        bondVlanId: '',
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
    setInventoryStatus('Refreshing inventory from SQLite...');
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
        const configResponse = await fetch('/api/v1/config');
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
              ? `${loadedNodes.length} node${loadedNodes.length === 1 ? '' : 's'} loaded from SQLite`
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
              ? `${loadedNodes.length} node${loadedNodes.length === 1 ? '' : 's'} loaded from SQLite`
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
    return () => {
      cancelled = true;
      window.clearInterval(refreshTimer);
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
    const response = await fetch('/api/v1/redfish/endpoints', {
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
    const response = await fetch('/api/v1/redfish/retrieve', {
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
      const response = await fetch('/api/v1/nodes', {
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
    setNodeConfigs(prev => ({ ...prev, [uuid]: { ...prev[uuid], [field]: value } }));
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
      const response = await fetch('/api/v1/nodes/assignments', {
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
      const response = await fetch(`/api/v1/nodes/${encodeURIComponent(node.uuid)}`, { method: 'DELETE' });
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
    setInventoryStatus('Committing reviewed inventory and preferences...');
    try {
      const saveResponse = await fetch('/api/v1/ansible-files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: nodeConfigs, blueprint, outputDirectory })
      });
      if (!saveResponse.ok) {
        const error = await saveResponse.json();
        throw new Error(error.error || `Ansible files could not be committed (HTTP ${saveResponse.status}).`);
      }
      const result = await saveResponse.json();
      setInventoryStatus(`Reviewed files committed. Inventory: ${result.inventoryFile}`);
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
                  outputDirectory={outputDirectory}
                  status={inventoryStatus}
                  error={inventoryError}
                  isRefreshing={isRefreshingInventory}
                  onRefresh={refreshInventory}
                  onBack={returnToInventory}
                  onConfirm={commitReviewedConfiguration}
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
              Built for disconnected environments. Your hardware data stays in your
              local SQLite-backed workspace.
            </p>
          </div>
          <div className="splash-workflow" aria-label="Workflow overview">
            <div className="splash-workflow-heading">A clear path to deployment</div>
            <div className="splash-flow-step">
              <span className="splash-step-number">01</span>
              <span className="splash-step-icon"><AutomationIcon /></span>
              <span><strong>Discover hardware</strong><small>IPA or secure Redfish</small></span>
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
          <span>Designed for OpenShift bare-metal installation planning</span>
          <span>Secrets stay in Ansible Vault — never in generated inventory</span>
        </footer>
      </div>
    </PageSection>
  );
}
function BlueprintStep({ blueprint, onChangeField, onChangeListField, onAddRegistry, onRegistryChange, onRemoveRegistry, outputDirectory, onOutputDirectoryChange, ansibleWorkingDir }) {
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
      <Gallery hasGutter minWidths={{ default: '430px' }}>
        {/* Card A: Identity Info */}
        <GalleryItem>
          <Card isPlain className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h2" size="lg">Identity & Topology</Title></CardHeader>
            <CardBody>
              <FormGroup label="Cluster Name" isRequired fieldId="bp-cluster-name"><TextInput id="bp-cluster-name" value={blueprint.clusterName} onChange={(_e, val) => onChangeField('clusterName', val)} /></FormGroup>
              <FormGroup label="Base Domain" isRequired fieldId="bp-base-domain" className="pf-v6-u-mt-md"><TextInput id="bp-base-domain" value={blueprint.baseDomain} onChange={(_e, val) => onChangeField('baseDomain', val)} placeholder="example.com" /></FormGroup>
              <FormGroup label="Topology" className="pf-v6-u-mt-md"><div style={{ display: 'flex', gap: '20px' }}><Radio id="topo-ha" name="t-g" label="Highly Available" isChecked={blueprint.controlPlaneTopology === 'HighlyAvailable'} onChange={() => onChangeField('controlPlaneTopology', 'HighlyAvailable')} /><Radio id="topo-sno" name="t-g" label="Compact SNO" isChecked={blueprint.controlPlaneTopology === 'SingleNode'} onChange={() => onChangeField('controlPlaneTopology', 'SingleNode')} /></div></FormGroup>
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
                labelHelp={<span>Subnet where the OpenShift hosts are connected, for example 192.168.10.0/24.</span>}
              >
                <TextInput
                  id="bp-machine-network-cidr"
                  value={blueprint.machineNetworkCidr || ''}
                  onChange={(_event, value) => onChangeField('machineNetworkCidr', value)}
                  placeholder="192.168.10.0/24"
                />
              </FormGroup>
              <FormGroup label="DNS servers" fieldId="bp-dns-servers">
                <TextArea
                  id="bp-dns-servers"
                  value={(blueprint.dnsServers || []).join('\n')}
                  onChange={(_event, value) => onChangeListField('dnsServers', value)}
                  placeholder={'192.168.10.10\n192.168.10.11'}
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
              <p>Keep pull secrets, BMC credentials, and other secrets in Ansible Vault. They are not collected or included in this export.</p>
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
      setStatus('Redfish BMC profiles saved in SQLite.');
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
  const unlinkedInventoryItems = inventoryItems;
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
            <span>{unlinkedInventoryItems.length} inventory names available</span>
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

        {discoveredNodes.length || unlinkedInventoryItems.length ? (
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
                        <div>{node.bmc_address || 'BMC address unavailable'}</div>
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
                {unlinkedInventoryItems.map((item) => {
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
      <Gallery hasGutter minWidths={{ default: '360px' }}>
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
                      </div>
                    ) : (
                      <div>Select an IPA node to view hardware</div>
                    )}
                  </div>
                  <FormGroup label="Designated Root Installation Disk" isRequired fieldId={`disk-${node.uuid}`} className="pf-v6-u-mb-md">
                    <FormSelect id={`disk-${node.uuid}`} value={cfg.rootDisk || ''} onChange={(_e, val) => onChangeField(node.uuid, 'rootDisk', val)} isDisabled={!hasHardware}>
                      <FormSelectOption value="" label="-- Choose target drive --" isDisabled />
                      {(selectedHardware?.inventory.disks || []).map(d => {
                        const speedTag = d.model.toLowerCase().includes('nvme') ? ' [Fastest: NVMe]' : d.model.toLowerCase().includes('ssd') ? ' [Fast: SSD]' : '';
                        return <FormSelectOption key={d.serial} value={d.serial} label={`${d.model}${speedTag} (${Math.round(d.size / 1000000000)} GB)`} />;
                      })}
                    </FormSelect>
                  </FormGroup>
                  <Title headingLevel="h4" size="md" className="pf-v6-u-mb-xs" style={{ display: 'flex', alignItems: 'center', color: '#151515' }}><NetworkIcon className="pf-v6-u-mr-xs" style={{ color: '#4f5255' }} /> Logical Interface Mapping</Title>
                  <div className="pf-v6-u-mb-sm" style={{ display: 'flex', gap: '20px' }}>
                    <Radio id={`net-single-${node.uuid}`} name={`net-${node.uuid}`} label="Standalone NIC" isChecked={cfg.netType === 'single'} onChange={() => onChangeField(node.uuid, 'netType', 'single')} isDisabled={!hasHardware} />
                    <Radio id={`net-bond-${node.uuid}`} name={`net-${node.uuid}`} label="Aggregated Bond" isChecked={cfg.netType === 'bond'} onChange={() => onChangeField(node.uuid, 'netType', 'bond')} isDisabled={!hasHardware} />
                  </div>
                  {cfg.netType === 'single' && (
                    <div style={{ border: '1px solid #d2d2d2', padding: '12px', borderRadius: '4px', backgroundColor: '#fafafa' }}>
                      <FormGroup label="Primary Logical Port" isRequired fieldId={`s-nic-${node.uuid}`} className="pf-v6-u-mb-sm">
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
                      <div style={{ borderTop: '1px dashed #c5c5c5' }} className="pf-v6-u-mt-sm pf-v6-u-pt-sm"><div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}><Checkbox label="Attach VLAN to bond0" id={`v-b-${node.uuid}`} isChecked={cfg.bondVlanEnabled || false} onChange={(_e, chk) => onChangeField(node.uuid, 'bondVlanEnabled', chk)} isDisabled={!hasHardware} />{cfg.bondVlanEnabled && <TextInput style={{ maxWidth: '90px', height: '26px', fontSize: '0.85rem' }} type="number" placeholder="VLAN ID" value={cfg.bondVlanId || ''} onChange={(_e, val) => onChangeField(node.uuid, 'bondVlanId', val)} />}</div></div>
                    </div>
                  )}
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

function InventoryReviewPage({ nodes, configs, outputDirectory, status, error, isRefreshing, onRefresh, onBack, onConfirm }) {
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
            {unnamedNodes.length > 0 && <p role="alert" style={{ color: '#a30000' }}>{unnamedNodes.length} node(s) need an Ansible host name before continuing.</p>}
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
              <div role="list" aria-label="All nodes in inventory" style={{ display: 'grid', gap: '8px' }}>
                {nodes.map((node) => {
                  const config = configs[node.uuid] || {};
                  const statusText = node.is_placeholder
                    ? 'Named host · waiting for hardware'
                    : `${node.source === 'redfish' ? 'Redfish' : 'IPA'} · discovered`;
                  return (
                    <div
                      key={node.uuid}
                      role="listitem"
                      style={{
                        display: 'grid',
                        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
                        gap: '8px 16px',
                        padding: '10px 12px',
                        border: '1px solid #d2d2d2',
                        borderRadius: '4px'
                      }}
                    >
                      <div><strong>Ansible host</strong><br />{config.ansibleHost || 'Not assigned'}</div>
                      <div><strong>Serial number</strong><br />{node.serial_number || (node.is_placeholder ? 'Awaiting discovery' : 'Not reported')}</div>
                      <div><strong>BMC address</strong><br />{node.bmc_address || 'Not reported'}</div>
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
        <div className="workflow-actions pf-v6-u-pt-lg pf-v6-u-mt-xl">
          <Button variant="secondary" onClick={onBack}>Back to inventory</Button>
          <Button variant="primary" onClick={onConfirm} isDisabled={!canConfirm} >
            {isCommitting ? 'Generating...' : 'Generate Ansible Files'}
          </Button>
        </div>
      </div>
    </PageSection>
  );
}

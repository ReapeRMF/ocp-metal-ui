import React, { useState, useEffect } from 'react';

// 1. Red Hat PatternFly Enterprise Scaffolding components
import { 
  Wizard, 
  WizardStep,
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
import { CheckCircleIcon, InfrastructureIcon, NetworkIcon, ClusterIcon, AddCircleOIcon, TrashIcon, AutomationIcon, KeyIcon } from '@patternfly/react-icons';

// Core stylesheets required for official compilation layout rules
import '@patternfly/react-core/dist/styles/base.css';
import '@patternfly/react-styles/css/components/Wizard/wizard.css';
import '@patternfly/react-styles/css/components/Card/card.css';
import '@patternfly/react-styles/css/components/Page/page.css';
import '@patternfly/react-styles/css/components/Masthead/masthead.css';

// Mock dataset expanded to 6 distinct bare-metal nodes discovered over network PXE via IPA
const initialIronicNodes = [
  {
    uuid: "8a4f91c2-38d1-4bda-912c-bbfa47821001",
    bmc_address: "10.10.50.21",
    inventory: {
      cpus: { count: 64, model: "AMD EPYC 7763" },
      memory: { total: 274877906944 }, // 256 GB
      disks: [
        { serial: "SEAGATE_HDD_9912A", model: "Seagate Mechanical HDD", size: 2000000000000 },
        { serial: "WDC_NVME_18428", model: "WD Black NVMe Drive", size: 500000000000 }
      ],
      interfaces: [{ name: "ens1f0", mac: "52:54:00:fa:19:bc" }, { name: "ens1f1", mac: "52:54:00:fa:19:bd" }]
    }
  },
  {
    uuid: "3c9a11e4-92c2-7fca-113d-ccba58932002",
    bmc_address: "10.10.50.22",
    inventory: {
      cpus: { count: 32, model: "Intel Xeon 6330" },
      memory: { total: 137438953472 }, // 128 GB
      disks: [{ serial: "CRUCIAL_SSD_5541", model: "Crucial SATA SSD", size: 480000000000 }],
      interfaces: [{ name: "ens1f0", mac: "52:54:00:aa:bb:cc" }]
    }
  },
  {
    uuid: "4b8c22d1-81b3-6eab-004a-bbaa55413003",
    bmc_address: "10.10.50.23",
    inventory: {
      cpus: { count: 64, model: "AMD EPYC 7763" },
      memory: { total: 274877906944 },
      disks: [{ serial: "SAMSUNG_NVME_1122", model: "Samsung Enterprise NVMe", size: 960000000000 }],
      interfaces: [{ name: "ens1f0", mac: "52:54:00:11:22:33" }]
    }
  },
  {
    uuid: "5d7e33f4-70a2-5dfc-112b-ddcc66324004",
    bmc_address: "10.10.50.24",
    inventory: {
      cpus: { count: 32, model: "Intel Xeon 6330" },
      memory: { total: 137438953472 },
      disks: [{ serial: "TOSHIBA_HDD_3344", model: "Toshiba Server HDD", size: 1000000000000 }],
      interfaces: [{ name: "ens1f0", mac: "52:54:00:77:88:99" }]
    }
  },
  {
    uuid: "6e2f44a5-61b9-4cfa-223c-eeff77415005",
    bmc_address: "10.10.50.25",
    inventory: {
      cpus: { count: 128, model: "AMD EPYC 9654" },
      memory: { total: 549755813888 }, // 512 GB
      disks: [{ serial: "MICRON_NVME_0011", model: "Micron NVMe Boot", size: 400000000000 }],
      interfaces: [{ name: "ens1f0", mac: "52:54:00:bb:cc:dd" }]
    }
  },
  {
    uuid: "7f1a55b6-52c8-3dfb-334d-ffaa88526006",
    bmc_address: "10.10.50.26",
    inventory: {
      cpus: { count: 64, model: "Intel Xeon 8480C" },
      memory: { total: 274877906944 },
      disks: [{ serial: "INTEL_SSD_7788", model: "Intel SATA SSD", size: 960000000000 }],
      interfaces: [{ name: "ens1f0", mac: "52:54:00:aa:11:22" }]
    }
  }
];
export default function App() {
  const [nodes] = useState(initialIronicNodes);
  const [nodeConfigs, setNodeConfigs] = useState({});
  const [pipelineStrategy, setPipelineStrategy] = useState('landing');

  // Updated blueprint state matrix carrying custom certificates and secure secret variables
  const [blueprint, setBlueprint] = useState({
    clusterName: 'ocp-baremetal',
    baseDomain: 'example.com',
    controlPlaneTopology: 'HighlyAvailable',
    pullSecret: '',
    mirrorRegistries: ['registry.airgap.internal:5000'],
    caCertBundle: '',       // Custom Certificate Authority Field
    vaultPassword: ''       // Automation Encrypted Secret Key Field
  });

  const [iloNodes, setIloNodes] = useState([
    { id: 1, ip: '10.10.50.11', username: 'admin', password: '' }
  ]);

  const updateBlueprintField = (field, value) => {
    setBlueprint(prev => ({ ...prev, [field]: value }));
  };

  const handleAddIloNode = () => {
    const newId = iloNodes.length > 0 ? Math.max(...iloNodes.map(n => n.id)) + 1 : 1;
    setIloNodes([...iloNodes, { id: newId, ip: '', username: '', password: '' }]);
  };

  const handleIloChange = (id, field, value) => {
    setIloNodes(iloNodes.map(node => node.id === id ? { ...node, [field]: value } : node));
  };

  const handleRemoveIloNode = (id) => {
    setIloNodes(iloNodes.filter(node => node.id !== id));
  };

  const findFastestDrive = (disks) => {
    if (!disks || disks.length === 0) return '';
    let bestDisk = disks;
    let bestScore = 0;
    disks.forEach(disk => {
      const modelLower = disk.model.toLowerCase();
      let currentScore = 1;
      if (modelLower.includes('nvme')) currentScore = 3;
      else if (modelLower.includes('ssd')) currentScore = 2;
      if (currentScore > bestScore || (currentScore === bestScore && disk.size > bestDisk.size)) {
        bestScore = currentScore;
        bestDisk = disk;
      }
    });
    return bestDisk.serial;
  };

  useEffect(() => {
    const initialConfigs = nodes.reduce((acc, node) => {
      acc[node.uuid] = {
        rootDisk: findFastestDrive(node.inventory.disks),
        netType: 'single',
        singleNic: node.inventory.interfaces?.name || '',
        singleVlanEnabled: false,
        singleVlanId: '',
        bondNics: [],
        bondVlanEnabled: false,
        bondVlanId: ''
      };
      return acc;
    }, {});
    setNodeConfigs(initialConfigs);
  }, [nodes]);

  const updateConfigField = (uuid, field, value) => {
    setNodeConfigs(prev => ({ ...prev, [uuid]: { ...prev[uuid], [field]: value } }));
  };

  const handleBondNicToggle = (uuid, nicName, isChecked) => {
    setNodeConfigs(prev => {
      const currentBonds = prev[uuid].bondNics || [];
      const updatedBonds = isChecked ? [...currentBonds, nicName] : currentBonds.filter(name => name !== nicName);
      return { ...prev, [uuid]: { ...prev[uuid], bondNics: updatedBonds } };
    });
  };

  if (Object.keys(nodeConfigs).length === 0) {
    return <p className="pf-v5-u-p-md">Loading execution parameters...</p>;
  }
  const header = (
    <Masthead backgroundColor="dark" style={{ borderBottom: '2px solid #ee0000' }}>
      <MastheadMain>
        <MastheadBrand style={{ color: '#ffffff', display: 'flex', alignItems: 'center', gap: '10px', textDecoration: 'none' }}>
          <ClusterIcon style={{ color: '#ee0000', fontSize: '1.4rem' }} />
          <span style={{ fontWeight: '700', letterSpacing: '-0.5px' }}>Red Hat</span>
          <span style={{ color: '#d2d2d2', fontWeight: '300' }}>OpenShift Bare-Metal Architect</span>
        </MastheadBrand>
      </MastheadMain>
    </Masthead>
  );

  const customSidebarFooter = (text) => (
    <div style={{ padding: '16px', borderTop: '1px solid #4f5255', width: '100%' }}>
      <div style={{ backgroundColor: '#212427', borderLeft: '4px solid #ee0000', padding: '12px', borderRadius: '4px' }}>
        <p style={{ color: '#ffffff', fontSize: '0.85rem', fontWeight: '700', marginBottom: '4px' }}>IRONIC ENGINE</p>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.8rem', color: '#d2d2d2' }}>
          <span style={{ height: '8px', width: '8px', backgroundColor: '#3e8635', borderRadius: '50%', display: 'inline-block' }}></span>
          {text}
        </div>
      </div>
    </div>
  );

  return (
    <Page header={header} hasNoSidebar={pipelineStrategy === 'landing'} style={{ height: '100vh' }}>
      <div style={{ height: 'calc(100vh - 70px)', display: 'flex', flexDirection: 'column' }}>
        
        {pipelineStrategy === 'landing' && (
          <StrategyLandingScreen onSelectStrategy={(strategy) => setPipelineStrategy(strategy)} />
        )}

        {pipelineStrategy === 'wizard-pxe' && (
          <Wizard title="Automated PXE Discovery Pipeline" className="openshift-architect-wizard" sidebarFooter={customSidebarFooter('PXE Active Loop')} onClose={() => setPipelineStrategy('landing')}>
            <WizardStep id="blueprint-step" name="1. Cluster Blueprint">
              <BlueprintStep blueprint={blueprint} onChangeField={updateBlueprintField} onAddRegistry={() => setBlueprint(p => ({ ...p, mirrorRegistries: [...p.mirrorRegistries, ''] }))} onRegistryChange={(idx, v) => setBlueprint(p => { const r = [...p.mirrorRegistries]; r[idx] = v; return { ...p, mirrorRegistries: r }; })} onRemoveRegistry={(idx) => setBlueprint(p => ({ ...p, mirrorRegistries: p.mirrorRegistries.filter((_, i) => i !== idx) }))} />
            </WizardStep>
            <WizardStep id="inventory-step" name="2. Hardware Discovery Matrix">
              <InventoryStep nodes={nodes} configs={nodeConfigs} onChangeField={updateConfigField} onBondToggle={handleBondNicToggle} onSave={() => alert("Profiles pushed successfully to Ironic core conductor tier!")} />
            </WizardStep>
          </Wizard>
        )}

        {pipelineStrategy === 'wizard-ilo' && (
          <Wizard title="Manual Out-of-Band iLO Pipeline" className="openshift-architect-wizard" sidebarFooter={customSidebarFooter('iLO Scanner Ready')} onClose={() => setPipelineStrategy('landing')}>
            <WizardStep id="blueprint-step" name="1. Cluster Blueprint">
              <BlueprintStep blueprint={blueprint} onChangeField={updateBlueprintField} onAddRegistry={() => setBlueprint(p => ({ ...p, mirrorRegistries: [...p.mirrorRegistries, ''] }))} onRegistryChange={(idx, v) => setBlueprint(p => { const r = [...p.mirrorRegistries]; r[idx] = v; return { ...p, mirrorRegistries: r }; })} onRemoveRegistry={(idx) => setBlueprint(p => ({ ...p, mirrorRegistries: p.mirrorRegistries.filter((_, i) => i !== idx) }))} />
            </WizardStep>
            <WizardStep id="onboarding-step" name="2. iLO Management Matrix">
              <IloManagementStep iloNodes={iloNodes} onAddIlo={handleAddIloNode} onIloChange={handleIloChange} onRemoveIlo={handleRemoveIloNode} />
            </WizardStep>
          </Wizard>
        )}

      </div>
    </Page>
  );
}

function StrategyLandingScreen({ onSelectStrategy }) {
  return (
    <PageSection variant="default" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', height: '100%', padding: '60px' }}>
      <div style={{ textAlign: 'center', maxWidth: '600px' }} className="pf-v5-u-mb-xl">
        <Title headingLevel="h1" size="2xl" className="pf-v5-u-mb-sm">Select Onboarding Strategy Blueprint</Title>
        <p style={{ color: '#4f5255' }}>Choose how OpenShift Ironic services should register and collect information about your bare-metal nodes before starting deployments.</p>
      </div>
      <Gallery hasGutter style={{ maxWidth: '900px', width: '100%' }}>
        <GalleryItem style={{ cursor: 'pointer' }} onClick={() => onSelectStrategy('wizard-pxe')}>
          <Card isFlat className="openshift-node-card" isSelectableRaised>
            <CardHeader><Title headingLevel="h3" size="lg"><AutomationIcon style={{ color: '#ee0000' }} className="pf-v5-u-mr-sm" /> Automated PXE Discovery Track</Title></CardHeader>
            <CardBody><p style={{ fontSize: '0.9rem', color: '#6a6e73' }}>Boot unknown hardware targets directly into the Ironic Python Agent ramdisk over the staging network. Nodes check in automatically with their hardware matrices fully mapped.</p></CardBody>
          </Card>
        </GalleryItem>
        <GalleryItem style={{ cursor: 'pointer' }} onClick={() => onSelectStrategy('wizard-ilo')}>
          <Card isFlat className="openshift-node-card" isSelectableRaised>
            <CardHeader><Title headingLevel="h3" size="lg"><InfrastructureIcon style={{ color: '#ee0000' }} className="pf-v5-u-mr-sm" /> Manual Out-of-Band iLO/BMC Track</Title></CardHeader>
            <CardBody><p style={{ fontSize: '0.9rem', color: '#6a6e73' }}>Explicitly define out-of-band management endpoint credentials. Scale out the server array using an adjustable profile row matrix before discovery kicks off.</p></CardBody>
          </Card>
        </GalleryItem>
      </Gallery>
    </PageSection>
  );
}
function BlueprintStep({ blueprint, onChangeField, onAddRegistry, onRegistryChange, onRemoveRegistry }) {
  return (
    <PageSection variant="default" style={{ overflowY: 'auto', flex: 1, padding: '24px', backgroundColor: '#f0f2f5' }}>
      <div className="pf-v5-u-mb-md">
        <Title headingLevel="h2" size="xl" className="pf-v5-u-mb-xs"><ClusterIcon className="pf-v5-u-mr-sm" style={{ color: '#ee0000' }} /> Global Cluster Blueprint</Title>
      </div>
      <Gallery hasGutter minWidths={{ default: '430px' }}>
        {/* Card A: Identity Info */}
        <GalleryItem>
          <Card isFlat className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h3" size="lg">Identity & Topology</Title></CardHeader>
            <CardBody>
              <FormGroup label="Cluster Name" isRequired fieldId="bp-cluster-name"><TextInput id="bp-cluster-name" value={blueprint.clusterName} onChange={(_e, val) => onChangeField('clusterName', val)} /></FormGroup>
              <FormGroup label="Base Domain" isRequired fieldId="bp-base-domain" className="pf-v5-u-mt-md"><TextInput id="bp-base-domain" value={blueprint.baseDomain} onChange={(_e, val) => onChangeField('baseDomain', val)} placeholder="example.com" /></FormGroup>
              <FormGroup label="Topology" className="pf-v5-u-mt-md"><div style={{ display: 'flex', gap: '20px' }}><Radio id="topo-ha" name="t-g" label="Highly Available" isChecked={blueprint.controlPlaneTopology === 'HighlyAvailable'} onChange={() => onChangeField('controlPlaneTopology', 'HighlyAvailable')} /><Radio id="topo-sno" name="t-g" label="Compact SNO" isChecked={blueprint.controlPlaneTopology === 'SingleNode'} onChange={() => onChangeField('controlPlaneTopology', 'SingleNode')} /></div></FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
        {/* Card B: Registry & Disconnected Air-Gap Mirrors */}
        <GalleryItem>
          <Card isFlat className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h3" size="lg">Registry Sync</Title></CardHeader>
            <CardBody>
              <FormGroup label="Pull Secret" fieldId="bp-pull-secret"><TextInput id="bp-pull-secret" value={blueprint.pullSecret} onChange={(_e, val) => onChangeField('pullSecret', val)} placeholder='{"auths":...}' /></FormGroup>
              <FormGroup label="Air-Gapped Mirror Registries" fieldId="bp-mirrors" className="pf-v5-u-mt-md">{blueprint.mirrorRegistries.map((registry, index) => (<div key={index} style={{ display: 'flex', gap: '10px', marginBottom: '8px' }}><TextInput style={{ flex: 1 }} placeholder="://example.com" value={registry} onChange={(_e, val) => onRegistryChange(index, val)} /><Button variant="danger" onClick={() => onRemoveRegistry(index)}>Remove</Button></div>))}<Button variant="secondary" onClick={onAddRegistry} className="pf-v5-u-mt-xs" style={{ borderColor: '#ee0000', color: '#ee0000' }}>+ Add Mirror Source</Button></FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
        {/* Card C: New Disconnected CA Cert Bundle & Ansible Vault Encryption Fields */}
        <GalleryItem>
          <Card isFlat className="openshift-node-card" style={{ height: '100%' }}>
            <CardHeader><Title headingLevel="h3" size="lg"><KeyIcon className="pf-v5-u-mr-xs" style={{ color: '#ee0000' }} /> Enterprise Security & CA Trust</Title></CardHeader>
            <CardBody>
              <FormGroup label="Additional Trust Bundle (Root CA Certificate PEM)" fieldId="bp-ca-certs">
                <TextArea style={{ resize: 'vertical', minHeight: '80px', fontFamily: 'monospace', fontSize: '0.85rem' }} id="bp-ca-certs" value={blueprint.caCertBundle} onChange={(_e, val) => onChangeField('caCertBundle', val)} placeholder="-----BEGIN CERTIFICATE-----\nMIID5TCCAs2gAwIBAgIJAK...\n-----END CERTIFICATE-----" />
              </FormGroup>
              <FormGroup label="Ansible Vault Secret Password" fieldId="bp-vault-pass" className="pf-v5-u-mt-md">
                <TextInput type="password" id="bp-vault-pass" value={blueprint.vaultPassword} onChange={(_e, val) => onChangeField('vaultPassword', val)} placeholder="••••••••••••••••" />
              </FormGroup>
            </CardBody>
          </Card>
        </GalleryItem>
      </Gallery>
    </PageSection>
  );
}

function IloManagementStep({ iloNodes, onAddIlo, onIloChange, onRemoveIlo }) {
  return (
    <PageSection variant="default" style={{ overflowY: 'auto', flex: 1, padding: '24px', backgroundColor: '#f0f2f5' }}>
      <Card isFlat className="openshift-node-card">
        <CardHeader style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Title headingLevel="h3" size="lg">Target BMC Target Management Matrix</Title>
          <Button variant="secondary" onClick={onAddIlo} style={{ borderColor: '#ee0000', color: '#ee0000' }} icon={<AddCircleOIcon />}>Add Node Profile</Button>
        </CardHeader>
        <CardBody>
          {iloNodes.map((node, index) => (
            <div key={node.id} style={{ display: 'flex', gap: '15px', alignItems: 'flex-end', paddingBottom: '12px', borderBottom: '1px solid #e0e0e0', marginTop: index > 0 ? '12px' : '0' }}>
              <FormGroup label={`Node #${index + 1} iLO IP`} fieldId={`ilo-ip-${node.id}`} style={{ flex: 2 }}><TextInput id={`ilo-ip-${node.id}`} value={node.ip} onChange={(_e, v) => onIloChange(node.id, 'ip', v)} placeholder="10.10.50.X" /></FormGroup>
              <FormGroup label="Username" fieldId={`ilo-user-${node.id}`} style={{ flex: 1 }}><TextInput id={`ilo-user-${node.id}`} value={node.username} onChange={(_e, v) => onIloChange(node.id, 'username', v)} /></FormGroup>
              <FormGroup label="Password" fieldId={`ilo-pass-${node.id}`} style={{ flex: 1 }}><TextInput type="password" id={`ilo-pass-${node.id}`} value={node.password} onChange={(_e, v) => onIloChange(node.id, 'password', v)} /></FormGroup>
              <Button variant="plain" onClick={() => onRemoveIlo(node.id)} style={{ color: '#a30000' }}><TrashIcon /></Button>
            </div>
          ))}
        </CardBody>
      </Card>
    </PageSection>
  );
}
function InventoryStep({ nodes, configs, onChangeField, onBondToggle, onSave }) {
  return (
    <PageSection variant="default" style={{ overflowY: 'auto', flex: 1, padding: '24px', backgroundColor: '#f0f2f5' }}>
      <div className="pf-v5-u-mb-md">
        <Title headingLevel="h2" size="xl" className="pf-v5-u-mb-xs"><InfrastructureIcon className="pf-v5-u-mr-sm" style={{ color: '#ee0000' }} /> Discovered Host Inventory Matrix</Title>
      </div>
      <Gallery hasGutter minWidths={{ default: '360px' }}>
        {nodes.map(node => {
          const cfg = configs[node.uuid] || {};
          return (
            <GalleryItem key={node.uuid}>
              <Card isFlat className="openshift-node-card">
                <CardHeader style={{ paddingBottom: '8px' }}>
                  <div><Title headingLevel="h3" size="lg" style={{ color: '#151515' }}><strong>Node Host: {node.bmc_address}</strong></Title><small style={{ color: '#6a6e73', fontFamily: 'monospace' }}>UUID: {node.uuid.substring(0, 8)}...</small></div>
                  <span style={{ color: '#8a0000', marginLeft: 'auto', fontSize: '0.85rem', fontWeight: '600', backgroundColor: '#ffe6e6', padding: '2px 8px', borderRadius: '2px' }}><CheckCircleIcon className="pf-v5-u-mr-xs" /> IPA Active</span>
                </CardHeader>
                <CardBody style={{ paddingTop: '8px' }}>
                  <div className="pf-v5-u-mb-md" style={{ backgroundColor: '#f0f2f5', padding: '10px', borderRadius: '4px', fontSize: '0.85rem', borderLeft: '3px solid #ee0000' }}><strong>Hardware:</strong> {node.inventory.cpus.count} Cores | {Math.round(node.inventory.memory.total / 1073741824)} GB RAM Allocated</div>
                  <FormGroup label="Designated Root Installation Disk" isRequired fieldId={`disk-${node.uuid}`} className="pf-v5-u-mb-md">
                    <FormSelect id={`disk-${node.uuid}`} value={cfg.rootDisk || ''} onChange={(_e, val) => onChangeField(node.uuid, 'rootDisk', val)}>
                      <FormSelectOption value="" label="-- Choose target drive --" isDisabled />
                      {node.inventory.disks.map(d => {
                        const speedTag = d.model.toLowerCase().includes('nvme') ? ' [Fastest: NVMe]' : d.model.toLowerCase().includes('ssd') ? ' [Fast: SSD]' : '';
                        return <FormSelectOption key={d.serial} value={d.serial} label={`${d.model}${speedTag} (${Math.round(d.size / 1000000000)} GB)`} />;
                      })}
                    </FormSelect>
                  </FormGroup>
                  <Title headingLevel="h4" size="md" className="pf-v5-u-mb-xs" style={{ display: 'flex', alignItems: 'center', color: '#151515' }}><NetworkIcon className="pf-v5-u-mr-xs" style={{ color: '#4f5255' }} /> Logical Interface Mapping</Title>
                  <div className="pf-v5-u-mb-sm" style={{ display: 'flex', gap: '20px' }}>
                    <Radio id={`net-single-${node.uuid}`} name={`net-${node.uuid}`} label="Standalone NIC" isChecked={cfg.netType === 'single'} onChange={() => onChangeField(node.uuid, 'netType', 'single')} />
                    <Radio id={`net-bond-${node.uuid}`} name={`net-${node.uuid}`} label="Aggregated Bond" isChecked={cfg.netType === 'bond'} onChange={() => onChangeField(node.uuid, 'netType', 'bond')} />
                  </div>
                  {cfg.netType === 'single' && (
                    <div style={{ border: '1px solid #d2d2d2', padding: '12px', borderRadius: '4px', backgroundColor: '#fafafa' }}>
                      <FormGroup label="Primary Logical Port" isRequired fieldId={`s-nic-${node.uuid}`} className="pf-v5-u-mb-sm"><FormSelect id={`s-nic-${node.uuid}`} value={cfg.singleNic || ''} onChange={(_e, val) => onChangeField(node.uuid, 'singleNic', val)}><FormSelectOption value="" label="-- Choose port --" isDisabled />{node.inventory.interfaces.map(i => <FormSelectOption key={i.name} value={i.name} label={`${i.name} [${i.mac}]`} />)}</FormSelect></FormGroup>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }} className="pf-v5-u-mt-sm"><Checkbox label="Enable VLAN Encapsulation" id={`v-s-${node.uuid}`} isChecked={cfg.singleVlanEnabled || false} onChange={(_e, chk) => onChangeField(node.uuid, 'singleVlanEnabled', chk)} />{cfg.singleVlanEnabled && <TextInput style={{ maxWidth: '90px', height: '26px', fontSize: '0.85rem' }} type="number" placeholder="VLAN ID" value={cfg.singleVlanId || ''} onChange={(_e, val) => onChangeField(node.uuid, 'singleVlanId', val)} />}</div>
                    </div>
                  )}
                  {cfg.netType === 'bond' && (
                    <div style={{ border: '1px solid #d2d2d2', padding: '12px', borderRadius: '4px', backgroundColor: '#f4f6f9' }}>
                      {node.inventory.interfaces.map(i => <Checkbox key={i.name} label={`${i.name} (${i.mac})`} id={`b-n-${node.uuid}-${i.name}`} isChecked={(cfg.bondNics || []).includes(i.name)} onChange={(_e, chk) => onBondToggle(node.uuid, i.name, chk)} className="pf-v5-u-mb-xs" />)}
                      <div style={{ borderTop: '1px dashed #c5c5c5' }} className="pf-v5-u-mt-sm pf-v5-u-pt-sm"><div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}><Checkbox label="Attach VLAN to bond0" id={`v-b-${node.uuid}`} isChecked={cfg.bondVlanEnabled || false} onChange={(_e, chk) => onChangeField(node.uuid, 'bondVlanEnabled', chk)} />{cfg.bondVlanEnabled && <TextInput style={{ maxWidth: '90px', height: '26px', fontSize: '0.85rem' }} type="number" placeholder="VLAN ID" value={cfg.bondVlanId || ''} onChange={(_e, val) => onChangeField(node.uuid, 'bondVlanId', val)} />}</div></div>
                    </div>
                  )}
                </CardBody>
              </Card>
            </GalleryItem>
          );
        })}
      </Gallery>
      <div style={{ borderTop: '1px solid #d2d2d2' }} className="pf-v5-u-pt-lg pf-v5-u-mt-xl"><Button variant="primary" onClick={onSave} style={{ backgroundColor: '#ee0000' }}>Save & Generate Host Configurations</Button></div>
    </PageSection>
  );
}


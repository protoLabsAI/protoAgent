import {expect} from '@playwright/test';
import {defineFlow} from '../../apps/web/e2e/flows/runner';
import {writeFileSync} from 'node:fs';
defineFlow({name:'no-model-verification',version:1,steps:[
{name:'Open completed console and assert missing model banner',async run(p){await p.addInitScript(()=>localStorage.removeItem('protoagent.authToken'));await p.goto('/app/');await expect(p.getByText("No model connected. The agent can't reply yet.",{exact:false})).toBeVisible();await expect(p.getByText('No messages in this session.',{exact:true})).toHaveCount(0);}},
{name:'Follow Connect a model to model setup',async run(p){await p.getByRole('button',{name:'Connect a model',exact:true}).click();await expect(p.getByText('Model & routing',{exact:true})).toBeVisible();await expect(p.getByRole('button',{name:'Add a connection',exact:true})).toBeVisible();writeFileSync(`${process.env.FLOW_OUT_DIR}/model-setup.txt`,await p.locator('body').innerText());writeFileSync(`${process.env.FLOW_OUT_DIR}/model-setup-url.txt`,p.url());}},
]});

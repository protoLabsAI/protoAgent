import {expect} from '@playwright/test';
import {defineFlow} from '../../apps/web/e2e/flows/runner';
import {writeFileSync} from 'node:fs';
defineFlow({name:'failed-connection',version:1,steps:[
{name:'Reopen isolated wizard for negative test',async run(p){await p.addInitScript(()=>localStorage.removeItem('protoagent.authToken'));await p.goto('/app/');await p.getByRole('button',{name:'Next',exact:true}).click();}},
{name:'Enter unreachable endpoint',async run(p){await p.getByLabel('Endpoint URL',{exact:true}).fill('http://127.0.0.1:1/v1');}},
{name:'Enter model and wait for failed automatic test',async run(p){await p.getByLabel('Model',{exact:true}).fill('audit-no-such-model');await expect(p.getByText('Connection failed — fix it to continue.',{exact:true})).toBeVisible({timeout:35000});await expect(p.getByRole('button',{name:'Next',exact:true})).toBeDisabled();writeFileSync(`${process.env.FLOW_OUT_DIR}/failed-connection.txt`,await p.locator('body').innerText());}},
]});

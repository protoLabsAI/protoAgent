import {expect} from '@playwright/test';
import {defineFlow} from '../../apps/web/e2e/flows/runner';
import {writeFileSync} from 'node:fs';
const dump=async(p:any,n:string)=>writeFileSync(`${process.env.FLOW_OUT_DIR}/${n}.txt`,await p.locator('body').innerText());
defineFlow({name:'acceptance',version:1,steps:[
{name:'Open fresh console',async run(p){await p.addInitScript(()=>localStorage.removeItem('protoagent.authToken'));await p.goto('/app/');await expect(p.getByRole('button',{name:'Next',exact:true})).toBeVisible();await dump(p,'welcome')}},
{name:'Welcome Next',async run(p){await p.getByRole('button',{name:'Next',exact:true}).click();await dump(p,'brain')}},
{name:'Choose Claude subscription',async run(p){await p.getByText('Claude subscription',{exact:true}).click();await dump(p,'claude')}},
{name:'Reach Claude sign in',async run(p){await p.getByRole('button',{name:'Sign in with Claude',exact:true}).click();await p.waitForTimeout(2000);await dump(p,'oauth');}},
{name:'Explore without a model',async run(p){await p.getByRole('button',{name:'Explore without a model',exact:true}).click();await dump(p,'agent')}},
{name:'Name agent',async run(p){await p.getByLabel('Agent name',{exact:true}).fill('Acceptance Audit');await dump(p,'named')}},
{name:'Review',async run(p){await p.getByRole('button',{name:'Next',exact:true}).click();await dump(p,'review')}},
{name:'Finish without model',async run(p){await p.getByRole('button',{name:'Finish',exact:true}).click();await p.waitForTimeout(3000);await dump(p,'finished');await expect(p.getByRole('button',{name:'Finish',exact:true})).toHaveCount(0)}},
]});

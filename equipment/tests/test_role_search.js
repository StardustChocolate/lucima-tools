/* 离线拼音规则回归，使用实际随包字典。运行 node equipment/tests/test_role_search.js。 */
const assert=require('node:assert/strict');
const {find}=require('../web/role-search.js');
const roles=[
 {id:'cat',name:'猫祭',aliases:['红猫'],staticID:'H212'},
 {id:'dailian',name:'爱恋泡影的玳莲',aliases:['泳莲'],staticID:'H192'},
 {id:'cypress',name:'奥柏丝蒂恩',aliases:[],staticID:'H501'},
 {id:'sound',name:'猫剂',aliases:[],staticID:'H888'},
 {id:'literal',name:'露娜',aliases:['月亮'],staticID:'H200'}
];
const ids=query=>find(roles,query).map(r=>r.id);
for(const word of ['猫祭','猫剂','猫级','毛季','maoji','MAO JI','ｍａｏｊｉ','mj','猫ji']){
 assert(ids(word).includes('cat'),word+' should find 猫祭');
}
assert.equal(ids('猫剂')[0],'sound','原文匹配排在谐音之前');
assert.deepEqual(ids('鸿茂'),['cat'],'别称也支持谐音');
assert.deepEqual(ids('待连'),['dailian'],'支持正式长名称中的部分谐音');
assert.deepEqual(ids('aobai'),['cypress']);
assert.deepEqual(ids('奥伯'),['cypress'],'人名多音字可按另一种读音查找');
assert.deepEqual(ids('h192'),['dailian'],'原有编号搜索保留');
assert.deepEqual(ids('月亮'),['literal']);
assert(!ids('aoji').includes('cat'),'不能从 mao 音节中间匹配 ao');
assert.deepEqual(ids('不存在的角色'),[]);
assert.deepEqual(find(roles,''),roles);
assert.deepEqual(find(roles.filter(r=>r.id!=='cat'),'猫级').map(r=>r.id),['sound'],'可选角色范围由调用方控制');
const bonds=[{id:'pool',name:'泳池畔私语',staticID:'A0302'},{id:'past',name:'酸苦的过往',staticID:'A0347'},{id:'sense',name:'新奇的感受',staticID:'A0131'}];
for(const query of ['酸苦','算苦','suanku','skdgw','酸ku','A0347'])assert(find(bonds,query).some(bond=>bond.id==='past'),'羁绊支持 '+query);
for(const query of ['泳池','泳迟','yongchi','ycpsy'])assert(find(bonds,query).some(bond=>bond.id==='pool'),'羁绊支持 '+query);
const both=JSON.stringify(roles);find(roles,'猫级');assert.equal(JSON.stringify(roles),both,'搜索不修改角色资料');
console.log('PASS: full names, aliases, IDs, homophones, pinyin, initials, polyphonic names, literal-first ordering and syllable boundaries.');
